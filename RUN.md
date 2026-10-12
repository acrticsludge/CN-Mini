# CN-Mini: Startup Commands

## 1. Clipboard — Ubuntu terminal

Run after Ubuntu starts to reactivate Windows ↔ Ubuntu copy-paste.

```bash
/usr/bin/VBoxClient --clipboard &
```

Make sure VirtualBox **Devices → Shared Clipboard → Bidirectional** is enabled.

## 2. Terminal 1 — Ryu SDN Controller

```bash
cd ~/CN-Mini && source .venv312/bin/activate && ryu-manager controller/sdn_controller.py
```

## 3. Terminal 2 — Mininet Topology

```bash
cd ~/CN-Mini && sudo python3 mininet/topology.py
```

When the `mininet>` prompt appears, test connectivity:

```text
pingall
```

## 4. Terminal 3 — Flask Dashboard

```bash
cd ~/CN-Mini && source .venv312/bin/activate && python frontend/app.py
```

Open the dashboard in Ubuntu at **http://localhost:5000**.

Keep the Ryu, Mininet, and Flask processes running while using the project.

## 5. Clear stored files for H3 and H4

Run this in an Ubuntu terminal **outside** the `mininet>` prompt:

```bash
cd ~/CN-Mini && sudo find stored_files/h3 stored_files/h4 -type f -delete 2>/dev/null
```

This deletes files inside `stored_files/h3` and `stored_files/h4`, while keeping the directories.

If you get a permission error or the paths do not exist, inspect the directory:

```bash
ls -la ~/CN-Mini/stored_files
```
