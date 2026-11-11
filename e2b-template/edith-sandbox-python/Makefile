
# 目标名里不能带冒号：GNU make 会把 `a:b:c:` 当成 static pattern rule，
# 直接报 "target pattern contains no '%'" 并中止（2026-11-11 在 Ubuntu 服务器上踩到）。
# 旧目标名 `e2b:build:dev` / `e2b:build:prod` 只在没人真跑 make 时「看起来正常」。
PYTHON ?= python3

.PHONY: build-dev build-prod

build-dev:
	$(PYTHON) build_dev.py

build-prod:
	$(PYTHON) build_prod.py
