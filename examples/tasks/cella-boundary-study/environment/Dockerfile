FROM ubuntu:24.04

WORKDIR /app

# sudo must be present for the task's sudoers grant to be usable by the agent.
RUN apt-get update && apt-get install -y sudo && rm -rf /var/lib/apt/lists/*
