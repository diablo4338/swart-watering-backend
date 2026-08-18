ENV_FILE ?= docker/.env
COMPOSE_FILE ?= docker/docker-compose.yml
CLI_ARGS ?=

COMPOSE = docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE)

.PHONY: config build up restart down cli

config:
	$(COMPOSE) config --quiet

build: config
	$(COMPOSE) build

up: config
	$(COMPOSE) up --build -d

restart: config
	$(COMPOSE) up --build --force-recreate -d

down:
	$(COMPOSE) down

cli:
	$(COMPOSE) exec cli python -m smart_watering $(CLI_ARGS)
