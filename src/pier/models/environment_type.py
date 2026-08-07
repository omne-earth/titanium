from enum import Enum


class EnvironmentType(str, Enum):
    DOCKER = "docker"
    GVISOR = "gvisor"
    MODAL = "modal"
    DAYTONA = "daytona"
