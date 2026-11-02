#!/bin/bash
set -e

echo "=== CN-Mini VM Setup & Edge Case Testing ==="
echo "Starting at $(date)"

cd ~/CN-Mini

# 1. Check environment
echo "--- Checking environment ---"
python3 --version
which pip3 || echo "pip3 not found"
which mininet || echo "mininet not found"
which ryu-manager || echo "ryu-manager not found"

# 2. Install system dependencies
echo "--- Installing system dependencies ---"
sudo apt-get update -qq
sudo apt-get install -y -qq mininet openvswitch-switch python3-pip python3-venv 2>&1 | tail -5

# 3. Install Python dependencies
echo "--- Installing Python dependencies ---"
pip3 install -r requirements.txt 2>&1 | tail -10

# 4. Verify installation
echo "--- Verifying installation ---"
which mininet
which ryu-manager
python3 -c "import flask; print('Flask:', flask.__version__)"
python3 -c "import ryu; print('Ryu OK')"

echo "=== Setup complete at $(date) ==="