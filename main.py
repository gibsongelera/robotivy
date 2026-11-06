#!/usr/bin/env python3
"""
TempBot - Humanoid Temperature Checking Robot
Convenience entry point wrapper.
"""
import os
import sys

# Ensure the raspberry_pi directory is in sys.path
pi_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "raspberry_pi")
if pi_dir not in sys.path:
    sys.path.insert(0, pi_dir)

if __name__ == "__main__":
    from tempbot import main
    main()
