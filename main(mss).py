import time
import numpy as np
import mss.windows
mss.windows.CAPTUREBLT = 0 # This flag captures layered windows (like games in borderless mode) but it causes a mouse flicker problem, also it can cause performance issues, so I disable it by default, but you can enable it if you want to capture those types of windows (just be aware of the potential performance hit) 
import gc
import mss
import cv2
import keyboard
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor
import math #for the cute animation when toggling off!

def main(): # Keep  everything in main to avoid global variables (slow) and make it easier to read, also makes it easier to add a GUI later if I want (which I probably won't but who knows)
    
    connected = False

    while not connected:
        try:
            client = OpenRGBClient('127.0.0.1', 6742)
            device = client.devices[0]
            connected = True
            print("Connected to OpenRGB successfully!")
        except Exception as e:
            print(f"Error connecting to OpenRGB: {e}")
            print("Retrying in 5 seconds...")
            time.sleep(5)

    sct = mss.mss()
    monitor = sct.monitors[1]

    enabled = True
    small = None

    def toggle():
        nonlocal small, enabled

        enabled = not enabled

        if not enabled:
            print("Lighting: OFF")
            # little heartbeat animation (runs once when turning OFF)
            steps = 40
            for i in range(steps):
                t = i / steps

                beat = 0.5 + 0.5 * math.sin(2 * math.pi * t)
                device.set_color(RGBColor(int(255 * beat), int(255 * beat), int(255 * beat)))
                time.sleep(0.02) # 20ms delay for smooth animation

            # delete frame buffer to free memory
            if small is not None:
                del small
                small = None
                gc.collect()

        else:
            print("Lighting: ON")

    keyboard.add_hotkey('ctrl+alt+l', toggle)
    keyboard.call_later(lambda: None)


    # ----- capture central region (60%) -----
    cx = monitor["width"] // 2
    cy = monitor["height"] // 2

    width = int(monitor["width"] * 0.6)
    height = int(monitor["height"] * 0.6)

    region = {
      "left": cx - width // 2,
      "top": cy - height // 2,
      "width": width,
      "height": height
    }

# --- Smoothing Variables ---
    prev_hue = 0 # Start with a default hue (red) to avoid abrupt changes on first run
    prev_v = 0 # Start with a default brightness to avoid abrupt changes on first run
    prev_s = 160 # Start with a moderate saturation to avoid dull colors
    target_h = 0
    target_s = 160
    target_v = 0
    r, g, b = 0, 0, 0 # Target colors
    alpha_val = 0.2  # Smoothing factor for brightness and saturation
    hsv_pixel = np.zeros((1,1,3), dtype=np.uint8) # Pre-allocate a pixel for HSV to RGB conversion (performance boost by avoiding repeated allocations)
    capture_counter = 0 # Counts frames to control capture frequency 
    CAPTURE_EVERY = 4  # Capture at ~15fps if loop is 60Hz (this took me so long to figure out, I hope it helps in optimizing performance)

    def circular_smooth(old, new, alpha=0.25):
        diff = ((new - old + 90) % 180) - 90 # Shortest path on a circle (finally school math use :D)
        return (old + alpha * diff) % 180
    

    while True:
        if not enabled:
            time.sleep(0.5)
            continue
        
        start_time = time.perf_counter()

        # 1. HEAVY LIFTING: Run only every 4th frame (~15 FPS)
        if capture_counter % CAPTURE_EVERY == 0:
            raw = sct.grab(region) 

            frame = np.frombuffer(raw.rgb, dtype=np.uint8) # Convert raw bytes to numpy array (much faster than PIL)
            frame = frame.reshape(raw.height, raw.width, 3) # Reshape to HWC format for OpenCV (much faster than PIL)
            if small is None:
                small = np.empty((45,80,3), dtype=np.uint8) # Pre-allocate small frame for performance (avoid reallocating every capture)

            cv2.resize(frame, (80,45), dst=small) # Resize to smaller frame for faster processing (this is a huge performance boost, I found 80x45 to be a good balance between performance and color accuracy, but you can experiment with this)
            
            hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
            h = hsv[:,:,0]
            s = hsv[:,:,1]
            v = hsv[:,:,2]

            total_pixels = h.size
            dark_mask = v < 40
            neutral_mask = (s < 35) & (v > 170) & (~dark_mask)
            neutral_ratio = np.sum(neutral_mask) / total_pixels

            if neutral_ratio > 0.35:
                # Smoothly transition to white
                target_v = 255
                target_s = 0
                # Hue doesn't matter for white, but we keep it stable

            else:
                color_mask = (s > 60) & (v > 80) & (~dark_mask)

                if np.any(color_mask):
                    hue_values = h[color_mask]
                    target_h = np.argmax(np.bincount(hue_values, minlength=180))

                # Brightness calc
                bright_pixels = v[v > 100]
                target_v = np.percentile(bright_pixels, 80) if len(bright_pixels) > 0 else np.mean(v)

                # Saturation calc
                sat_pixels = s[s > 50]
                target_s = np.percentile(sat_pixels, 75) if len(sat_pixels) > 0 else 160



        # Smoothly step the previous values towards the targets
        prev_hue = circular_smooth(prev_hue, target_h, alpha=0.15)
        prev_v = prev_v + alpha_val * (target_v - prev_v)
        prev_s = prev_s + alpha_val * (target_s - prev_s)

        # Convert smoothed HSV back to BGR for the hardware
        hsv_pixel[0,0,0] = int(prev_hue)
        hsv_pixel[0,0,1] = int(prev_s)
        hsv_pixel[0,0,2] = int(prev_v)
        color_final = cv2.cvtColor(hsv_pixel, cv2.COLOR_HSV2RGB)[0,0]
        b, g, r = color_final

        # 2. LIGHT LIFTING: Update Hardware every loop (~60 FPS)
        # This ensures the smoothing happens at a high frequency for ultra-smooth transitions, while the heavy capture + processing happens less frequently to save CPU
        device.set_color(RGBColor(int(r), int(g), int(b)))

        capture_counter += 1

        # Target 60 FPS loop for ultra-smooth transitions :D
        elapsed = time.perf_counter() - start_time
        time.sleep(max(0, 0.0166 - elapsed)) 

if __name__ == "__main__": # Classic Python entry point :D 
    main()
    
