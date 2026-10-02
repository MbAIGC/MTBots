PY ?= python3
export PYTHONPATH := $(CURDIR)/.vendor:$(CURDIR)

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

# 不想让向导 ssh 出去时：照这三条在远端把用户/公钥/守卫装好，再回 make add-host 写清单
remote-setup:
	@echo "把下面 3 条里的 <远端> 换成你的地址；第 1、2 条在 MTBots 这台机器上跑："
	@echo ""
	@echo "  scp ./data/ssh/id_ed25519.pub <远端>:/tmp/mtbots.pub"
	@echo "  scp docs/examples/mtbots-compose-guard.sh <远端>:/tmp/guard.sh"
	@echo "  ssh <远端> 'sudo sh -s -- --user mtbots --pubkey /tmp/mtbots.pub --guard /tmp/guard.sh' < docs/examples/mtbots-remote-setup.sh"
	@echo ""
	@echo "（第 3 条会把脚本喂给远端的 sudo sh，一次跑完：建用户 + docker 组 + 家目录权限 + 装守卫 + 写 authorized_keys）"

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
