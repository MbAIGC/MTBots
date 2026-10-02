PY ?= python3
export PYTHONPATH := $(CURDIR)/.vendor:$(CURDIR)
VERSION := $(shell $(PY) -c "import mtbots; print(mtbots.__version__)" 2>/dev/null || echo 0.0.0)
# 脚本 URL 默认走 main（不用跟着版本号改）；要锁版本：make remote-setup REF=v$(VERSION)
REF ?= main

.PHONY: help check health test run list add-host remote-setup fmt clean

help:
	@echo "make check   — 配置自检（不连 Telegram）"
	@echo "make health  — 配置自检 + docker compose / LitePan 连通性探测"
	@echo "make test    — 跑单元测试（stdlib unittest）"
	@echo "make run      — 启动 Bot"
	@echo "make list    — 列出已启用模块"
	@echo "make add-host — 一键接入远端主机（交互式向导，容器里跑）"
	@echo "make remote-setup — 只在远端装用户/公钥/守卫（把命令打印出来，你自己贴到远端跑）"

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

# 一键接入远端主机：向导会问远端地址/账号，然后建用户、装守卫、写公钥、写主机清单并验证
add-host:
	docker compose exec mtbots sh /app/scripts/setup-remote-host.sh

# 远端那台自己跑（bot 这边没有能 ssh 过去的账号/root 时用）：打印短命令 + 要粘的公钥
RAW := https://raw.githubusercontent.com/MbAIGC/MTBots/$(REF)
remote-setup:
	@pub=$$(cat ./data/ssh/id_ed25519.pub 2>/dev/null || echo '（本机还没有密钥：先在项目根跑 make add-host 生成）'); \
	echo "在【远端主机】上以 root 跑这一条（跑起来它会问你账号和公钥）："; \
	echo ""; \
	echo "  sudo bash <(curl -fsSL $(RAW)/docs/examples/mtbots-remote-setup.sh)"; \
	echo ""; \
	echo "问「公钥」时把下面这行粘进去："; \
	echo ""; \
	echo "  $$pub"; \
	echo ""; \
	echo "（要锁版本：make remote-setup REF=v$(VERSION)）"

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
