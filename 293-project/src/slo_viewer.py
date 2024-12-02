import curses
import time
import numpy as np
from datetime import datetime
import signal
import sys
import ray
from collections import defaultdict

class SLOViewer:
    def __init__(self):
        ray.init(address='auto')
        self.model_map = {
            hash('vit') % 1000: ('vit', 50),
            hash('resnet') % 1000: ('resnet', 200),
            hash('shufflenet') % 1000: ('shufflenet', 30),
            hash('efficientnet') % 1000: ('efficientnet', 40)
        }
        self.metrics_buffer = defaultdict(lambda: {'latencies': [], 'timestamps': []})
        
    def get_metrics(self, slo_queue, window_size=60):
        current_time = time.time()
        cutoff_time = current_time - window_size
        
        # Get all new metrics from queue
        while slo_queue.qsize() > 0:
            try:
                timestamp, model_id, latency = slo_queue.get_nowait()
                if model_id in self.model_map:
                    model_name = self.model_map[model_id][0]
                    self.metrics_buffer[model_name]['latencies'].append(latency)
                    self.metrics_buffer[model_name]['timestamps'].append(timestamp)
            except:
                break
        
        # Process metrics within window
        metrics = {}
        for model_name, data in self.metrics_buffer.items():
            recent_indices = [i for i, t in enumerate(data['timestamps']) 
                            if t > cutoff_time]
            
            if not recent_indices:
                continue
                
            recent_latencies = np.array([data['latencies'][i] for i in recent_indices])
            slo = self.model_map[hash(model_name) % 1000][1]
            
            metrics[model_name] = {
                'slo': slo,
                'count': len(recent_latencies),
                'rps': len(recent_latencies) / window_size,
                'avg_latency': np.mean(recent_latencies),
                'p50_latency': np.percentile(recent_latencies, 50),
                'p95_latency': np.percentile(recent_latencies, 95),
                'p99_latency': np.percentile(recent_latencies, 99),
                'max_latency': np.max(recent_latencies),
                'slo_violation_rate': (np.sum(recent_latencies > slo) / 
                                     len(recent_latencies)) * 100
            }
            
            # Clean up old data
            data['latencies'] = [data['latencies'][i] for i in recent_indices]
            data['timestamps'] = [data['timestamps'][i] for i in recent_indices]
            
        return metrics

def display_monitor(stdscr, viewer):
    curses.start_color()
    curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
    curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
    curses.init_pair(3, curses.COLOR_YELLOW, curses.COLOR_BLACK)
    
    def signal_handler(sig, frame):
        curses.endwin()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    slo_queue = ray.get_actor("slo_tracker").get_queue.remote()

    while True:
        try:
            metrics = viewer.get_metrics(slo_queue)
            stdscr.clear()
            
            header = f"Real-time SLO Monitor - {datetime.now().strftime('%H:%M:%S')}"
            stdscr.addstr(0, 0, header, curses.A_BOLD)
            stdscr.addstr(1, 0, "=" * len(header))

            if not metrics:
                stdscr.addstr(3, 0, "No data available - waiting for requests...")
                stdscr.refresh()
                time.sleep(1)
                continue

            row = 3
            for model_name, data in metrics.items():
                color = (curses.color_pair(1) if data['slo_violation_rate'] > 5 else 
                        curses.color_pair(3) if data['slo_violation_rate'] > 1 else 
                        curses.color_pair(2))

                stdscr.addstr(row, 0, f"\nModel: {model_name}", curses.A_BOLD)
                row += 2
                
                metrics_str = (
                    f"SLO: {data['slo']}ms | "
                    f"Violations: {data['slo_violation_rate']:.1f}% | "
                    f"RPS: {data['rps']:.1f}"
                )
                stdscr.addstr(row, 2, metrics_str, color)
                row += 1

                latency_str = (
                    f"Latency (ms) - "
                    f"Avg: {data['avg_latency']:.1f} | "
                    f"P50: {data['p50_latency']:.1f} | "
                    f"P95: {data['p95_latency']:.1f} | "
                    f"P99: {data['p99_latency']:.1f} | "
                    f"Max: {data['max_latency']:.1f}"
                )
                stdscr.addstr(row, 2, latency_str)
                row += 2

            stdscr.addstr(row, 0, "-" * len(header))
            row += 1
            stdscr.addstr(row, 0, "Press Ctrl+C to exit")
            
            stdscr.refresh()
            time.sleep(0.5)

        except Exception as e:
            stdscr.clear()
            stdscr.addstr(0, 0, f"Error: {str(e)}")
            stdscr.refresh()
            time.sleep(1)

if __name__ == "__main__":
    viewer = SLOViewer()
    curses.wrapper(lambda stdscr: display_monitor(stdscr, viewer))