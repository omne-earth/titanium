from enum import Enum


class EnvironmentType(str, Enum):
    DOCKER = "docker"
    GVISOR = "gvisor"
    GVISOR_PODMAN = "gvisor-podman"
    PODMAN = "podman"
    MODAL = "modal"
    DAYTONA = "daytona"
