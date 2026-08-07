from enum import Enum


class EnvironmentType(str, Enum):
    DOCKER = "docker"
    GVISOR = "gvisor"
    PODMAN = "podman"
    MODAL = "modal"
    DAYTONA = "daytona"
