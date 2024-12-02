import json
import time
from datetime import datetime
import os

def clear_screen():
    """Clear screen cross-platform"""
    os.system('cls' if os.name == 'nt' else 'clear')

def display_metrics():
    """Display metrics from shared file with enhanced debugging"""
    metrics_file = "metrics.json"
    
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
                    print(f"\nModel: {model_name}")
                    print("-" * 40)
                    
                    total = stats['total_requests']
                    violations = stats['slo_violations']
                    compliance = 100.0 if total == 0 else ((total - violations) / total * 100)
                    
                    print(f"  Queue Size: {stats['queue_size']}")
                    print(f"  Total Requests: {total:,}")
                    print(f"  Dropped Requests: {stats['dropped_requests']:,}")
                    print(f"  SLO Violations: {violations:,}")
                    print(f"  SLO Compliance: {compliance:.2f}%")
                    print(f"  Last Latency: {stats.get('last_latency', 0):.2f}ms")
                    print(f"  Average Latency: {stats['avg_latency']:.2f}ms")
                    print(f"  P95 Latency: {stats['p95_latency']:.2f}ms")
                    print(f"  P99 Latency: {stats['p99_latency']:.2f}ms")
                    
                    # Status indicators with more granular thresholds
                    queue_status = "✓" if stats['queue_size'] < 1600 else "!"
                    slo_status = "✓" if compliance >= 98 else "!" if compliance >= 95 else "✗"
                    
                    print(f"  Status: Queue={queue_status} SLO={slo_status}")
                
                print("\n" + "=" * 50)
                print("Status: ✓ Good  ! Warning  ✗ Critical")
                
            time.sleep(1)
            
        except Exception as e:
            print(f"Error reading metrics: {e}")
            time.sleep(1)

if __name__ == "__main__":
    display_metrics()