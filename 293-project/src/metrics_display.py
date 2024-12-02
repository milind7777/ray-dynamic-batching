import json
import time
from datetime import datetime
import os
import argparse

def parse_args():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='Display real-time SLO metrics')
    parser.add_argument('--simple', action='store_true', 
                       help='Display only SLO compliance and average latency')
    return parser.parse_args()

def clear_screen():
    """Clear screen cross-platform"""
    os.system('cls' if os.name == 'nt' else 'clear')

def display_metrics():
    """Display metrics from shared file"""
    metrics_file = "metrics.json"
    args = parse_args()
    
    while True:
        try:
            if os.path.exists(metrics_file):
                with open(metrics_file, 'r') as f:
                    metrics = json.load(f)
                
                clear_screen()
                print("=== Real-time SLO Metrics ===")
                print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print("=" * 50)
                
                for model_name, stats in metrics.items():
                    # Skip models with zero requests
                    if stats['total_requests'] == 0:
                        continue
                    
                    print(f"\nModel: {model_name}")
                    print("-" * 40)
                    
                    total = stats['total_requests']
                    violations = stats['slo_violations']
                    compliance = 100.0 if total == 0 else ((total - violations) / total * 100)
                    
                    if args.simple:
                        # Display simplified metrics
                        print(f"  SLO Compliance: {compliance:.2f}%")
                        print(f"  Average Latency: {stats['avg_latency']:.2f}ms")
                    else:
                        # Display full metrics
                        print(f"  Queue Size: {stats['queue_size']}")
                        print(f"  Total Requests: {total:,}")
                        print(f"  Dropped Requests: {stats['dropped_requests']:,}")
                        print(f"  SLO Violations: {violations:,}")
                        print(f"  SLO Compliance: {compliance:.2f}%")
                        print(f"  Last Latency: {stats.get('last_latency', 0):.2f}ms")
                        print(f"  Average Latency: {stats['avg_latency']:.2f}ms")
                        print(f"  P95 Latency: {stats['p95_latency']:.2f}ms")
                        print(f"  P99 Latency: {stats['p99_latency']:.2f}ms")
                        
                        # Status indicators
                        queue_status = "✓" if stats['queue_size'] < 1600 else "!"
                        slo_status = "✓" if compliance >= 98 else "!" if compliance >= 95 else "✗"
                        print(f"  Status: Queue={queue_status} SLO={slo_status}")
                
                print("\n" + "=" * 50)
                if not args.simple:
                    print("Status: ✓ Good  ! Warning  ✗ Critical")
                
            time.sleep(1)
            
        except Exception as e:
            print(f"Error reading metrics: {e}")
            time.sleep(1)

if __name__ == "__main__":
    display_metrics()