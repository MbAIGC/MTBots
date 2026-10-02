PY ?= python3
export PYTHONPATH := $(CURDIR)/.vendor:$(CURDIR)
VERSION := $(shell $(PY) -c "import mtbots; print(mtbots.__version__)" 2>/dev/null || echo 0.0.0)

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

# 远端那台自己跑（bot 这边没有能 ssh 过去的账号/root 时用）：打印一条可直接粘贴的命令
HOST_USER ?= mtbots
REF ?= v$(VERSION)
RAW := https://raw.githubusercontent.com/MbAIGC/MTBots/$(REF)
remote-setup:
	@pub=$$(cat ./data/ssh/id_ed25519.pub 2>/dev/null || echo 'ssh-ed25519 AAAA…（把 ./data/ssh/id_ed25519.pub 的内容粘到这里）'); \
	echo "在【远端主机】上以 root 跑这一条（自己下守卫、建用户、加 docker 组、写 authorized_keys）："; \
	echo ""; \
	echo "curl -fsSL $(RAW)/docs/examples/mtbots-remote-setup.sh | sudo sh -s -- --user $(HOST_USER) --guard-url $(RAW)/docs/examples/mtbots-compose-guard.sh --pubkey-line '$$pub'"; \
	echo ""; \
	echo "（要换账号：make remote-setup HOST_USER=admin；要固定其它版本：make remote-setup REF=v1.3.0）"

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
