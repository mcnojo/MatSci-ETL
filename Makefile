.PHONY: infra infra-down worker run

infra:
	docker compose up -d

infra-down:
	docker compose down

worker:
	python -m prod.worker

run:
	@echo "Usage: python -m etl.cli --pdf <path>"
