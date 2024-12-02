import curses
import time
import numpy as np
from multiprocessing import shared_memory
from datetime import datetime
import signal
import sys

class SLOViewer:
    def __init__(self, max_entries=10000):
        try:
            self.shm = shared_memory.SharedMemory(name="slo_metrics")
            self.buffer = np.ndarray((max_entries, 3), dtype=np.float64, buffer=self.shm.buf)
            
            self.model_map = {
                hash('vit') % 1000: ('vit', 50),
                hash('resnet') % 1000: ('resnet', 200),
                hash('shufflenet') % 1000: ('shufflenet', 30),
                hash('efficientnet') % 1000: ('efficientnet', 40)
            }
            
        except FileNotFoundError:
            raise Exception("Shared memory 'slo_metrics' not found. Is the scheduler running?")

    def get_metrics(self, window_size=60):
        current_time = time.time()
        cutoff_time = current_time - window_size
        
        # Get recent data
        recent_data = self.buffer[self.buffer[:, 0] > cutoff_time]
        if len(recent_data) == 0:
            return {}

        metrics = {}
        for model_id, (model_name, slo) in self.model_map.items():
            model_data = recent_data[recent_data[:, 2] == model_id]
            if len(model_data) == 0:
                continue
                
            latencies = model_data[:, 1]
            metrics[model_name] = {
                'slo': slo,
                'count': len(latencies),
                'rps': len(latencies) / window_size,
                'avg_latency': np.mean(latencies),
                'p50_latency': np.percentile(latencies, 50),
                'p95_latency': np.percentile(latencies, 95),
                'p99_latency': np.percentile(latencies, 99),
                'max_latency': np.max(latencies),
                'slo_violation_rate': (np.sum(latencies > slo) / len(latencies)) * 100
            }
        
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

    while True:
        try:
            metrics = viewer.get_metrics()
            stdscr.clear()
            
            # Header
            current_time = datetime.now().strftime("%H:%M:%S")
            header = f"Real-time SLO Monitor - {current_time}"
            stdscr.addstr(0, 0, header, curses.A_BOLD)
            stdscr.addstr(1, 0, "=" * len(header))

            if not metrics:
                stdscr.addstr(3, 0, "No data available - waiting for requests...")
                stdscr.refresh()
                time.sleep(1)
                continue

            # Display metrics for each model
            row = 3
            for model_name, data in metrics.items():
                # Color based on SLO violation rate
                if data['slo_violation_rate'] > 5:
                    color = curses.color_pair(1)
                elif data['slo_violation_rate'] > 1:
                    color = curses.color_pair(3)
                else:
                    color = curses.color_pair(2)

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

            # Footer
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

def main():
    try:
        viewer = SLOViewer()
        curses.wrapper(lambda stdscr: display_monitor(stdscr, viewer))
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()