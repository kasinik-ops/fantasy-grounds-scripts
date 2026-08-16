#!/usr/bin/env python3
"""
fgu_telemetry.py — Real-time performance monitor for Fantasy Grounds Unity.

This script attaches to the running Fantasy Grounds process and logs 
its CPU and Memory (RAM) consumption to a CSV file every 0.5 seconds. 
Perfect for correlating static map metrics to actual VTT engine strain.
"""

import psutil
import time
import csv
import sys
from datetime import datetime

# The target process name on macOS/Windows
TARGET_PROCESS = "Fantasy Grounds"

def find_fgu_process():
    """Scans active OS processes to find Fantasy Grounds."""
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            name = proc.info['name'] or ""
            cmdline = " ".join(proc.info['cmdline'] or [])
            # Catch both the binary name and the macOS .app execution path
            if TARGET_PROCESS.lower() in name.lower() or TARGET_PROCESS.lower() in cmdline.lower():
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
    return None

def main():
    print(f"Searching for '{TARGET_PROCESS}' process...")
    fgu_proc = find_fgu_process()

    if not fgu_proc:
        print("❌ Fantasy Grounds is not currently running.")
        print("Please start FGU, load your campaign, and run this script again.")
        sys.exit(1)

    print(f"✅ Attached to Fantasy Grounds (PID: {fgu_proc.pid})")
    
    # Initialize CPU percentage (the first call always returns 0.0)
    fgu_proc.cpu_percent(interval=None)
    
    # Generate a unique CSV filename based on the current time
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"fgu_telemetry_{timestamp}.csv"
    
    with open(filename, mode='w', newline='') as file:
        writer = csv.writer(file)
        # Write CSV Headers
        writer.writerow(["Timestamp", "CPU_Percent", "RAM_MB"])
        
        print(f"📊 Logging telemetry to {filename}")
        print("Press Ctrl+C to stop recording and save the file.\n")
        print("-" * 40)
        print(f"{'Time':<15} | {'CPU %':<10} | {'RAM (MB)':<10}")
        print("-" * 40)
        
        try:
            while True:
                # Halt if the user closes FGU mid-test
                if not fgu_proc.is_running():
                    print("\n❌ Fantasy Grounds was closed. Stopping telemetry.")
                    break
                    
                # Gather real-time metrics
                current_time = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                cpu = fgu_proc.cpu_percent(interval=None) # CPU usage since last loop
                ram_mb = fgu_proc.memory_info().rss / (1024 * 1024)
                
                # Write to file and update the console on the same line
                writer.writerow([current_time, f"{cpu:.1f}", f"{ram_mb:.1f}"])
                print(f"{current_time:<15} | {cpu:<10.1f} | {ram_mb:<10.1f}", end='\r')
                
                # Sample rate: 500ms
                time.sleep(0.5)  
                
        except KeyboardInterrupt:
            # Graceful exit on Ctrl+C
            print("\n\n🛑 Telemetry stopped by user.")
            print(f"Data saved successfully to: {filename}")

if __name__ == "__main__":
    main()
