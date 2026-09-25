PY ?= python3
export PYTHONPATH := $(CURDIR)/.vendor:$(CURDIR)

.PHONY: help check health test run list fmt clean

help:
	@echo "make check   — 配置自检（不连 Telegram）"
	@echo "make health  — 配置自检 + docker compose / LitePan 连通性探测"
	@echo "make test    — 跑单元测试（stdlib unittest）"
	@echo "make run      — 启动 Bot"
	@echo "make list    — 列出已启用模块"

check:
	$(PY) -m mtbots --check

health:
	$(PY) -m mtbots --health

list:
	$(PY) -m mtbots --list

test:
	$(PY) -m unittest discover -s tests -t . -v

run:
	$(PY) -m mtbots

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
