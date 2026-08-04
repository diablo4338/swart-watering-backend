ENV_FILE ?= docker/.env
COMPOSE_FILE ?= docker/docker-compose.local.yml
PRODUCTION_COMPOSE_FILE ?= docker/docker-compose.yml
BUILD_COMPOSE_FILE ?= docker/docker-compose.build.yml
SERVICES ?= smart-watering worker snapshotter watering-detector public-api cli
PUBLISH_SERVICES ?= smart-watering worker public-api cli
CLI_ARGS ?=

COMPOSE = docker compose --env-file $(ENV_FILE) -f $(COMPOSE_FILE)
PUBLISH_COMPOSE = docker compose --env-file $(ENV_FILE) \
	-f $(PRODUCTION_COMPOSE_FILE) -f $(BUILD_COMPOSE_FILE)

.PHONY: build restart publish cli

build:
	$(COMPOSE) config --quiet
	$(COMPOSE) build $(SERVICES)

restart:
	$(COMPOSE) config --quiet
	$(COMPOSE) down
	$(COMPOSE) build $(SERVICES)
	$(COMPOSE) up --force-recreate -d $(SERVICES)

publish:
	$(PUBLISH_COMPOSE) config --quiet
	$(PUBLISH_COMPOSE) build $(PUBLISH_SERVICES)
	$(PUBLISH_COMPOSE) push $(PUBLISH_SERVICES)

cli:
	$(COMPOSE) exec cli python -m smart_watering $(CLI_ARGS)
