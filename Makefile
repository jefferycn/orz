build:
	docker buildx build --platform linux/amd64 -t jefferycn/orz . --push
