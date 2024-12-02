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
        ray.init(address='auto', namespace='SLOMonitroing')
        self.model_map = {
            hash('vit') % 1000: ('vit', 50),
            hash('resnet') % 1000: ('resnet', 200),
            hash('shufflenet') % 1000: ('shufflenet', 30),
            hash('efficientnet') % 1000: ('efficientnet', 40)
        }
        self.metrics_buffer = defaultdict(lambda: {
            'latencies': [], 
            'timestamps': [],
            'request_ids': []
        })
        
    def get_metrics(self, slo_queue, window_size=60):
        current_time = time.time()
        cutoff_time = current_time - window_size
        
        while slo_queue.qsize() > 0:
            try:
                timestamp, model_id, latency, request_id = slo_queue.get_nowait()
                if model_id in self.model_map:
                    model_name = self.model_map[model_id][0]
                    self.metrics_buffer[model_name]['latencies'].append(latency)
                    self.metrics_buffer[model_name]['timestamps'].append(timestamp)
                    self.metrics_buffer[model_name]['request_ids'].append(request_id)
            except:
                break
        
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
                'min_latency': np.min(recent_latencies),
                'slo_violation_rate': (np.sum(recent_latencies > slo) / 
                                     len(recent_latencies)) * 100,
                'unique_requests': len(set(
                    data['request_ids'][i] for i in recent_indices
                ))
            }
            
            # Cleanup old data
            for key in ['latencies', 'timestamps', 'request_ids']:
                data[key] = [data[key][i] for i in recent_indices]
            
        return metrics

def display_monitor(stdscr, viewer, actor_name):
    print("Displaying real-time SLO monitor. Press Ctrl+C to exit.")
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_RED, -1)
    curses.init_pair(2, curses.COLOR_GREEN, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    
    def signal_handler(sig, frame):
        curses.endwin()
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)

    try:
        slo_queue = ray.get_actor(actor_name).get_queue.remote()
    except:
        stdscr.addstr(0, 0, "Error: Cannot connect to SLO tracker. Is the scheduler running?")
        stdscr.refresh()
        time.sleep(3)
        sys.exit(1)

    while True:
        try:
            metrics = viewer.get_metrics(slo_queue)
            stdscr.clear()
            
            # Header
            header = f"Real-time SLO Monitor - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
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
                    color = curses.color_pair(1)  # Red
                elif data['slo_violation_rate'] > 1:
                    color = curses.color_pair(3)  # Yellow
                else:
                    color = curses.color_pair(2)  # Green

                # Model header
                stdscr.addstr(row, 0, f"\nModel: {model_name}", curses.A_BOLD)
                row += 2
                
                # SLO metrics
                slo_str = (
                    f"SLO Target: {data['slo']}ms | "
                    f"Violations: {data['slo_violation_rate']:.1f}% | "
                    f"Requests/sec: {data['rps']:.1f}"
                )
                stdscr.addstr(row, 2, slo_str, color)
                row += 1

                # Latency metrics
                latency_str = (
                    f"Latency (ms) - "
                    f"Min: {data['min_latency']:.1f} | "
                    f"Avg: {data['avg_latency']:.1f} | "
                    f"P50: {data['p50_latency']:.1f} | "
                    f"P95: {data['p95_latency']:.1f} | "
                    f"P99: {data['p99_latency']:.1f} | "
                    f"Max: {data['max_latency']:.1f}"
                )
                stdscr.addstr(row, 2, latency_str)
                row += 1

                # Request counts
                count_str = (
                    f"Requests in window: {data['count']} | "
                    f"Unique requests: {data['unique_requests']}"
                )
                stdscr.addstr(row, 2, count_str)
                row += 2

            # Footer
            stdscr.addstr(row, 0, "-" * len(header))
            row += 1
            footer = [
                "🟢 Good (<1% violations)",
                "🟡 Warning (1-5% violations)",
                "🔴 Critical (>5% violations)",
                "Press Ctrl+C to exit"
            ]
            for line in footer:
                stdscr.addstr(row, 0, line)
                row += 1
            
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
        # Find the SLO tracker by searching actors
        actors = ray.state.actors()
        slo_tracker_id = None
        actor = {}
        for actor_id, info in actors.items():
            if info['ActorClassName'] == 'SharedSLOTracker' and info['Name'] == 'slo_tracker':
                slo_tracker_id = actor_id
                actor = info
                break
                
        if not slo_tracker_id:
            print("SLO tracker not found in active actors")
            sys.exit(1)
            
        slo_tracker = ray.get_actor(actor['Name'])
        print(f"Found SLO tracker actor: {actor['Name']} (ID: {slo_tracker_id})")

            

        curses.wrapper(lambda stdscr: display_monitor(stdscr, viewer, actor['Name']))
    except KeyboardInterrupt:
        print("\nExiting...")
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()