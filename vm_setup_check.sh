#!/bin/bash
# CN-Mini VM Full Setup + Edge Case Test Runner
# Run from inside the Ubuntu VM: bash <path-to-this-script>
set -e

echo "=============================================="
echo "CN-Mini VM Edge Case Testing"
echo "Started: $(date)"
echo "=============================================="

cd ~/CN-Mini

# --- Step 1: Find shared folder mount point ---
SHARED_DIR=""
for d in /media/sf_Anubhav /media/sf_anubhav /mnt/hgfs/Anubhav /mnt/hgfs/anubhav; do
    if [ -d "$d" ]; then
        SHARED_DIR="$d"
        break
    fi
done

if [ -z "$SHARED_DIR" ]; then
    echo "Trying to mount VirtualBox shared folder 'Anubhav'..."
    sudo mkdir -p /media/sf_Anubhav
    sudo mount -t vboxsf Anubhav /media/sf_Anubhav 2>/dev/null && SHARED_DIR="/media/sf_Anubhav" || true
fi

if [ -z "$SHARED_DIR" ]; then
    echo "ERROR: Could not find/mount shared folder. CN-Mini may not be accessible."
    echo "Falling back to ~/CN-Mini"
    SHARED_DIR="$HOME/CN-Mini"
else
    echo "Shared folder found at: $SHARED_DIR"
fi

# --- Step 2: Check what's installed ---
echo ""
echo "--- Checking dependencies ---"
VENV_OK=0
MININET_OK=0
RYU_OK=0

[ -f "$HOME/CN-Mini/.venv312/bin/activate" ] && VENV_OK=1 || true
which mininet >/dev/null 2>&1 && MININET_OK=1 || true
which ryu-manager >/dev/null 2>&1 && RYU_OK=1 || true

echo "venv312: $([ $VENV_OK -eq 1 ] && echo OK || echo MISSING)"
echo "mininet: $([ $MININET_OK -eq 1 ] && echo OK || echo MISSING)"
echo "ryu-manager: $([ $RYU_OK -eq 1 ] && echo OK || echo MISSING)"

# --- Step 3: Install missing dependencies ---
if [ $VENV_OK -eq 0 ] || [ $MININET_OK -eq 0 ] || [ $RYU_OK -eq 0 ]; then
    echo ""
    echo "--- Installing dependencies ---"
    sudo apt-get update -qq
    sudo apt-get install -y -qq mininet openvswitch-switch python3-pip python3-venv 2>&1 | tail -5
    
    if [ $VENV_OK -eq 0 ]; then
        echo "Creating venv312..."
        python3 -m venv $HOME/CN-Mini/.venv312
    fi
    
    source $HOME/CN-Mini/.venv312/bin/activate
    pip3 install -r requirements.txt 2>&1 | tail -10
fi

# --- Step 4: Verify ---
echo ""
echo "--- Verification ---"
source $HOME/CN-Mini/.venv312/bin/activate
which mininet && which ryu-manager
python3 -c "import flask; print('Flask OK')"
python3 -c "from ryu.base import app_manager; print('Ryu OK')"

echo ""
echo "=== Setup complete. Ready to run tests. ==="
echo "To start the system, open 3 terminals and run:"
echo "  T1: ryu-manager controller/sdn_controller.py"
echo "  T2: sudo python3 mininet/topology.py"
echo "  T3: python frontend/app.py"