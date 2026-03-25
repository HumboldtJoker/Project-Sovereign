.PHONY: install run autonomous scan lint check

install:
	pip install -r requirements.txt

run:
	python main.py --query "Analyze the top 3 opportunities in the current market"

autonomous:
	python main.py --autonomous

scan:
	python main.py --scan

lint:
	python -m py_compile main.py
	find . -name "*.py" -not -path "*/__pycache__/*" -exec python -m py_compile {} \;
	@echo "All files compile clean"

check:
	@echo "Modules:" && find . -name "*.py" -not -path "*/__pycache__/*" -not -name "__init__.py" | wc -l
	@echo "Lines:" && find . -name "*.py" -not -path "*/__pycache__/*" | xargs wc -l | tail -1
	@echo "Compile check:" && make lint
