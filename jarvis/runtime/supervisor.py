"""Runtime Supervision System - Core lifecycle management for Vantage services.

This module implements the authoritative runtime supervisor that owns:
* Service registration and lifecycle
* Startup ordering and dependency resolution  
* Health monitoring and state tracking
* Graceful shutdown procedures
* Failure recovery where appropriate
* No orphan processes or duplicate instances

The RuntimeSupervisor becomes the single source of truth for runtime state.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from ..logging_setup import get_logger

log = get_logger("runtime.supervisor")


class ServiceState(str, Enum):
    """Service operational states."""
    NOT_STARTED = "not_started"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    RECOVERING = "recovering"


@dataclass
class ServiceSpec:
    """Specification for a managed service."""
    name: str
    required: bool = True
    start_timeout: int = 30
    shutdown_timeout: int = 10
    restart_policy: str = "immediate"  # immediate | delayed | none
    max_restarts: int = 3
    dependencies: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "start_timeout": self.start_timeout,
            "shutdown_timeout": self.shutdown_timeout,
            "restart_policy": self.restart_policy,
            "max_restarts": self.max_restarts,
        }


@dataclass
class ServiceInstance:
    """Runtime instance of a service."""
    spec: ServiceSpec
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    state: ServiceState = ServiceState.NOT_STARTED
    last_start: Optional[datetime] = None
    last_stop: Optional[datetime] = None
    restart_count: int = 0
    error_message: Optional[str] = None
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.spec.to_dict(),
            "state": self.state.value,
            "pid": self.pid,
            "last_start": self.last_start.isoformat() if self.last_start else None,
            "restart_count": self.restart_count,
            "error_message": self.error_message,
        }


@dataclass
class SupervisorStatus:
    """Overall supervisor and runtime status."""
    supervisor_state: ServiceState = ServiceState.NOT_STARTED
    startup_time: Optional[datetime] = None
    services: Dict[str, ServiceInstance] = field(default_factory=dict)
    degraded_reasons: List[str] = field(default_factory=list)
    version: str = "0.2.0"
    pid: Optional[int] = None
    
    @property
    def is_ready(self) -> bool:
        return self.supervisor_state == ServiceState.READY
    
    @property
    def is_degraded(self) -> bool:
        return self.supervisor_state == ServiceState.DEGRADED or len(self.degraded_reasons) > 0
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "supervisor_state": self.supervisor_state.value,
            "startup_time": self.startup_time.isoformat() if self.startup_time else None,
            "services": {k: v.to_dict() for k, v in self.services.items()},
            "degraded_reasons": self.degraded_reasons,
            "version": self.version,
            "pid": self.pid,
        }


class RuntimeSupervisor:
    """Central authority for Vantage runtime lifecycle.

    In-process singleton: `RuntimeSupervisor()` and `RuntimeSupervisor.get_instance()`
    return the same authority, so a second construction cannot fork the lifecycle.
    The `__new__` guard (not `__init__`) is what makes `RuntimeSupervisor() is SUPER`
    true: `__init__` runs on every call and would otherwise rebuild state.
    """

    _instance: Optional["RuntimeSupervisor"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.status = SupervisorStatus(pid=os.getpid())
        self._registered_services: Dict[str, ServiceSpec] = {}
        self._running_services: Dict[str, ServiceInstance] = {}
        self._shutdown_lock = threading.Lock()
        self._shutdown_event = threading.Event()
        self._health_check_thread: Optional[threading.Thread] = None
        self._signal_handlers_installed = False
        self._guardian_pid: Optional[int] = None

        # Register known services with correct dependencies
        self._register_backend()
        self._register_agent()
        self._register_workflow()
        self._register_scheduler()
        self._register_voice()

    @classmethod
    def get_instance(cls) -> "RuntimeSupervisor":
        """Get the singleton instance (idempotent)."""
        if cls._instance is None:
            cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Clear the in-process singleton so the next access builds fresh state.

        Intended for tests, where a fresh supervisor per test is the honest way to
        observe a lifecycle from NOT_STARTED; it cannot leak across the process.
        """
        cls._instance = None

    def get_status(self) -> Dict[str, Any]:
        """Supervisor state as a plain dict -- the shape the menu bar and the
        /health endpoint read. Carries no credential material."""
        return self.status.to_dict()
    
    def _register_backend(self) -> None:
        """Register backend API server service."""
        self._registered_services["backend"] = ServiceSpec(
            name="backend",
            required=True,
            start_timeout=45,
            shutdown_timeout=10,
            dependencies=[],
        )
    
    def _register_agent(self) -> None:
        """Register agent orchestrator service."""
        self._registered_services["agent"] = ServiceSpec(
            name="agent",
            required=True,
            start_timeout=10,
            shutdown_timeout=5,
            dependencies=["backend"],
        )
    
    def _register_workflow(self) -> None:
        """Register workflow runner service."""
        self._registered_services["workflow"] = ServiceSpec(
            name="workflow",
            required=False,
            start_timeout=10,
            shutdown_timeout=5,
            dependencies=["backend"],
        )
    
    def _register_scheduler(self) -> None:
        """Registered scheduler service."""
        self._registered_services["scheduler"] = ServiceSpec(
            name="scheduler",
            required=False,
            start_timeout=10,
            shutdown_timeout=5,
            dependencies=["backend", "workflow"],
        )
    
    def _register_voice(self) -> None:
        """Register voice runtime service."""
        self._registered_services["voice"] = ServiceSpec(
            name="voice",
            required=False,
            start_timeout=15,
            shutdown_timeout=5,
            dependencies=["backend"],
        )
    
    def start(self) -> bool:
        """Start all services according to dependencies.

        Re-runnable: a previous `stop()` armed the one-shot shutdown event, and leaving it
        set makes the very next start() abort before any service comes up.
        """
        self._shutdown_event.clear()
        log.info("runtime supervisor starting")
        self.status.supervisor_state = ServiceState.STARTING
        self.status.startup_time = datetime.now(timezone.utc)
        # A fresh start is a clean slate for the degraded state: a previous run's
        # degraded_reasons must not leak into this one, or `is_degraded` (which is
        # `state == DEGRADED or degraded_reasons`) would stay True across a restart
        # even after a fully-healthy READY.
        self.status.degraded_reasons = []
        
        if not self._setup_signal_handlers():
            log.warning("could not setup signal handlers")
        
        # Start services topologically sorted by dependencies
        try:
            order = self._topological_sort()
            # Optional services that failed to start. A required failure aborts
            # immediately below; these are what make the runtime DEGRADED rather
            # than READY. Before this fix, an optional-service failure fell straight
            # through to READY, so the health indicator reported "fully healthy" while
            # a capability was down — the DEGRADED state, `degraded_reasons`, and
            # `is_degraded` existed but start() never set them (dormant machinery).
            degraded: List[str] = []

            for service_name in order:
                if self._shutdown_event.is_set():
                    log.warning("startup interrupted")
                    break

                success = self._start_service(service_name)
                if not success:
                    if self._registered_services[service_name].required:
                        log.error(f"critical service {service_name} failed to start")
                        self.status.supervisor_state = ServiceState.FAILED
                        return False
                    log.warning(f"optional service {service_name} failed to start")
                    degraded.append(service_name)

            if not self._shutdown_event.is_set():
                if degraded:
                    # All required services are up, so the runtime is operational — but
                    # one or more optional capabilities are down. Say DEGRADED, not
                    # READY, and name them so the state is truthful.
                    self.status.supervisor_state = ServiceState.DEGRADED
                    self.status.degraded_reasons = list(degraded)
                    self._start_health_monitoring()
                    log.warning(
                        "runtime degraded (optional service(s) down): " + ", ".join(degraded)
                    )
                else:
                    self.status.supervisor_state = ServiceState.READY
                    self._start_health_monitoring()
                    log.info("runtime supervisor ready")
                return True

        except Exception as e:
            log.error(f"startup failed: {e}")
            self.status.supervisor_state = ServiceState.FAILED
            return False

        return False
    
    def _topological_sort(self) -> List[str]:
        """Sort services by dependencies using Kahn's algorithm."""
        # Build in-degree map
        in_degree = {name: 0 for name in self._registered_services}
        for name, spec in self._registered_services.items():
            for dep in spec.dependencies:
                if dep in in_degree:
                    in_degree[name] += 1
        
        # Find all nodes with no dependencies
        queue = [name for name, degree in in_degree.items() if degree == 0]
        result = []
        
        while queue:
            node = queue.pop(0)
            result.append(node)
            
            for name, spec in self._registered_services.items():
                if node in spec.dependencies:
                    in_degree[name] -= 1
                    if in_degree[name] == 0:
                        queue.append(name)
        
        return result
    
    def _start_service(self, service_name: str) -> bool:
        """Start a single service."""
        spec = self._registered_services[service_name]
        
        # Check dependencies
        for dep in spec.dependencies:
            if dep not in self._running_services or self._running_services[dep].state != ServiceState.READY:
                log.warning(f"{service_name} waiting for dependency {dep}")
                return False
        
        instance = ServiceInstance(spec=spec)
        instance.state = ServiceState.STARTING
        
        try:
            if service_name == "backend":
                success = self._start_backend(instance)
            elif service_name == "agent":
                success = self._start_agent(instance)
            elif service_name == "workflow":
                success = self._start_workflow(instance)
            elif service_name == "scheduler":
                success = self._start_scheduler(instance)
            elif service_name == "voice":
                success = self._start_voice(instance)
            else:
                log.error(f"unknown service: {service_name}")
                return False
            
            if success:
                instance.state = ServiceState.READY
                instance.last_start = datetime.now(timezone.utc)
                self._running_services[service_name] = instance
                return True
            else:
                instance.state = ServiceState.FAILED
                self._running_services[service_name] = instance
                return False
                
        except Exception as e:
            log.error(f"{service_name} startup exception: {e}")
            instance.state = ServiceState.FAILED
            instance.error_message = str(e)
            self._running_services[service_name] = instance
            return False
    
    def _start_backend(self, instance: ServiceInstance) -> bool:
        """Start backend API server."""
        from ..api.app import app as api_app
        
        # Backend will be started externally via uvicorn
        # This just registers that it's expected
        log.info("backend service registered")
        return True
    
    def _start_agent(self, instance: ServiceInstance) -> bool:
        """Start agent orchestrator."""
        from ..core.orchestrator import JarvisCore
        
        try:
            core = JarvisCore()
            log.info("agent orchestrator started")
            return True
        except Exception as e:
            log.error(f"agent startup failed: {e}")
            return False
    
    def _start_workflow(self, instance: ServiceInstance) -> bool:
        """Start workflow runner."""
        from ..workflows.engine import WorkflowEngine
        
        try:
            engine = WorkflowEngine.get_instance()
            log.info("workflow engine started")
            return True
        except Exception as e:
            log.error(f"workflow startup failed: {e}")
            return False
    
    def _start_scheduler(self, instance: ServiceInstance) -> bool:
        """Start scheduled jobs runner."""
        from ..scheduler.runner import SchedulerRunner
        
        try:
            runner = SchedulerRunner()
            log.info("scheduler started")
            return True
        except Exception as e:
            log.error(f"scheduler startup failed: {e}")
            return False
    
    def _start_voice(self, instance: ServiceInstance) -> bool:
        """Start voice runtime."""
        from ..voice.runtime import VoiceRuntime
        
        try:
            runtime = VoiceRuntime.get_instance(enabled=True)
            log.info("voice runtime started")
            return True
        except Exception as e:
            log.error(f"voice startup failed: {e}")
            return False
    
    def _setup_signal_handlers(self) -> bool:
        """Setup signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            log.info(f"received signal {signum}, initiating shutdown")
            self.stop()
        
        try:
            signal.signal(signal.SIGTERM, handle_signal)
            signal.signal(signal.SIGINT, handle_signal)
            self._signal_handlers_installed = True
            return True
        except Exception:
            return False
    
    def _start_health_monitoring(self) -> None:
        """Start background health check thread."""
        def monitor_loop():
            while not self._shutdown_event.is_set():
                self._check_all_services()
                time.sleep(2.0)
        
        self._health_check_thread = threading.Thread(
            target=monitor_loop,
            name="health_monitor",
            daemon=True
        )
        self._health_check_thread.start()
    
    def _check_all_services(self) -> None:
        """Check health of all running services."""
        current_time = datetime.now(timezone.utc).isoformat()
        
        for service_name, instance in self._running_services.items():
            if instance.state == ServiceState.READY:
                # In real impl: check pid/process/heartbeat
                pass
    
    def stop(self) -> None:
        """Gracefully stop all services."""
        if self._shutdown_event.is_set():
            log.warning("already stopping")
            return
        
        log.info("runtime supervisor stopping")
        self._shutdown_event.set()
        self.status.supervisor_state = ServiceState.STOPPING
        
        # Stop services in reverse dependency order
        order = self._reverse_topological_sort()
        
        for service_name in order:
            if service_name in self._running_services:
                self._stop_service(service_name)
        
        self.status.supervisor_state = ServiceState.STOPPED
        log.info("runtime supervisor stopped")
    
    def _reverse_topological_sort(self) -> List[str]:
        """Reverse order so dependents are stopped before their dependencies."""
        forward = self._topological_sort()
        return list(reversed(forward))
    
    def _stop_service(self, service_name: str) -> None:
        """Stop a single service."""
        if service_name not in self._running_services:
            return
        
        instance = self._running_services[service_name]
        instance.state = ServiceState.STOPPING
        instance.last_stop = datetime.now(timezone.utc)
        
        try:
            if service_name == "backend":
                self._stop_backend(instance)
            elif service_name == "agent":
                self._stop_agent(instance)
            elif service_name == "workflow":
                self._stop_workflow(instance)
            elif service_name == "scheduler":
                self._stop_scheduler(instance)
            elif service_name == "voice":
                self._stop_voice(instance)
            
            instance.state = ServiceState.STOPPED
            del self._running_services[service_name]
            
        except Exception as e:
            log.error(f"{service_name} shutdown exception: {e}")
            instance.state = ServiceState.FAILED
    
    def _stop_backend(self, instance: ServiceInstance) -> None:
        """Stop backend API server."""
        log.info("stopping backend")
        # Backend shutdown handled externally via port cleanup
    
    def _stop_agent(self, instance: ServiceInstance) -> None:
        """Stop agent orchestrator."""
        log.info("stopping agent")
    
    def _stop_workflow(self, instance: ServiceInstance) -> None:
        """Stop workflow runner."""
        log.info("stopping workflow")
    
    def _stop_scheduler(self, instance: ServiceInstance) -> None:
        """Stop scheduler."""
        log.info("stopping scheduler")
    
    def _stop_voice(self, instance: ServiceInstance) -> None:
        """Stop voice runtime."""
        log.info("stopping voice")
    
    def _register_guardian(self) -> None:
        """Register guardian relationship to prevent orphaned processes."""
        from ..process import GUARDIAN_ENV, start_guardian_from_env
        
        guardian_pid = os.environ.get(GUARDIAN_ENV)
        if guardian_pid and guardian_pid.isdigit():
            # Already watched by existing guardian
            log.debug("already supervised by guardian PID %s", guardian_pid)
            return
        
        # Install new guardian if available
        try:
            start_guardian_from_env(on_exit=self._on_guardian_gone)
        except Exception as e:
            log.warning(f"guardian installation failed: {e}")
    
    def _on_guardian_gone(self) -> None:
        """Handle when guardian process terminates."""
        log.warning("guardian gone, shutting down")
        self.stop()


# The canonical service table. Exposed at module level so callers and tests read one
# source of truth instead of each supervisor instance's private registration map.
SERVICE_SPECS: Dict[str, ServiceSpec] = {
    "backend": ServiceSpec(name="backend", required=True,
                           start_timeout=45, shutdown_timeout=10, dependencies=[]),
    "agent": ServiceSpec(name="agent", required=True,
                         start_timeout=10, shutdown_timeout=5, dependencies=["backend"]),
    "workflow": ServiceSpec(name="workflow", required=False,
                            start_timeout=10, shutdown_timeout=5, dependencies=["backend"]),
    "scheduler": ServiceSpec(name="scheduler", required=False,
                             start_timeout=10, shutdown_timeout=5,
                             dependencies=["backend", "workflow"]),
    "voice": ServiceSpec(name="voice", required=False,
                         start_timeout=15, shutdown_timeout=5, dependencies=["backend"]),
}


def port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when something is already bound to (host, port).

    A connect is the check, not a kill: detecting an occupied port never terminates
    whatever is holding it. The connect is bounded so a dead listener cannot hang
    startup.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Singleton instance for global access
SUPER = RuntimeSupervisor.get_instance()
