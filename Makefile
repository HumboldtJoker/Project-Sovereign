.PHONY: setup install run autonomous scan lint check clean

setup:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	@echo "\nSetup complete. Activate with: source .venv/bin/activate"
	@echo "Then copy .env.example to .env and add your API keys."

install:
	pip install -r requirements.txt

run:
	python3 main.py --query "Analyze the top 3 opportunities in the current market"

autonomous:
	python3 main.py --autonomous

scan:
	python3 main.py --scan

lint:
	python3 -m py_compile main.py
	find . -name "*.py" -not -path "*/__pycache__/*" -not -path "./.venv/*" -exec python3 -m py_compile {} \;
	@echo "All files compile clean"

check:
	@echo "Modules:" && find . -name "*.py" -not -path "*/__pycache__/*" -not -path "./.venv/*" -not -name "__init__.py" | wc -l
	@echo "Lines:" && find . -name "*.py" -not -path "*/__pycache__/*" -not -path "./.venv/*" | xargs wc -l | tail -1
	@echo "Compile check:" && make lint

clean:
	find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete 2>/dev/null; true
