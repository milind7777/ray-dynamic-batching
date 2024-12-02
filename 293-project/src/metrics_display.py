
import json
import time
from datetime import datetime
import os

def clear_screen():
    """Clear screen cross-platform"""
    os.system('cls' if os.name == 'nt' else 'clear')

def display_metrics():
    """Display metrics from shared file"""
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
                    
                    # Calculate SLO compliance
                    compliance = 100.0
                    if stats['total_requests'] > 0:
                        compliance = ((stats['total_requests'] - stats['slo_violations']) / 
                                    stats['total_requests'] * 100)
                    
                    print(f"  Queue Size: {stats['queue_size']}")
                    print(f"  Total Requests: {stats['total_requests']}")
                    print(f"  Dropped Requests: {stats['dropped_requests']}")
                    print(f"  SLO Violations: {stats['slo_violations']}")
                    print(f"  SLO Compliance: {compliance:.2f}%")
                    print(f"  Average Latency: {stats['avg_latency']:.2f}ms")
                    print(f"  P95 Latency: {stats['p95_latency']:.2f}ms")
                    
                    # Status indicators
                    queue_status = "✓" if stats['queue_size'] < 1600 else "!"  # 80% of 2000
                    slo_status = "✓" if compliance >= 95 else "!" if compliance >= 90 else "✗"
                    print(f"  Queue Status: {queue_status}  SLO Status: {slo_status}")
                
                print("\n" + "=" * 50)
                print("Status: ✓ Good  ! Warning  ✗ Critical")
            
            time.sleep(1)
            
        except Exception as e:
            print(f"Error reading metrics: {e}")
            time.sleep(1)

if __name__ == "__main__":
    display_metrics()