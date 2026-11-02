#!/bin/bash
cd ~/CN-Mini
source .venv312/bin/activate
echo "=== TEST RESULTS ===" > /tmp/test_results.txt
echo "Date: $(date)" >> /tmp/test_results.txt
echo "" >> /tmp/test_results.txt
pytest tests/ -v 2>&1 | tee -a /tmp/test_results.txt
echo "" >> /tmp/test_results.txt
echo "=== EXIT CODE: $? ===" >> /tmp/test_results.txt
echo "DONE"