# Copyright 2023 by Garmin Ltd. or its subsidiaries
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import time
import shlex
import re
import subprocess
import logging
import os
from os import getenv, path
from pathlib import Path

import allure
import pytest
from pytest_harvest import get_fixture_store

logger = logging.getLogger(__name__)

device = getenv("LG_ENV", "Unknown").split("/")[-1].split(".")[0]


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    result = outcome.get_result()

    if result.when == "call":
        allure.dynamic.parent_suite(device)


def pytest_addoption(parser):
    parser.addoption("--firmware", action="store", default="firmware.bin")
    parser.addoption("--flash-firmware", action="store_true", 
                     help="Flash firmware before running tests")
    parser.addoption("--flash-keep-config", action="store_true",
                     help="Keep configuration when flashing")
    parser.addoption("--flash-verify-version", action="store", default=None,
                     help="Verify firmware version after flashing")


def pytest_sessionfinish(session):
    """Gather all results and save them to a JSON file."""

    fixture_store = get_fixture_store(session)
    if "results_bag" not in fixture_store:
        return

    results = fixture_store["results_bag"]

    Path("results.json").write_text(json.dumps(results, indent=2))

    alluredir = session.config.getoption("--alluredir")

    if not alluredir or not path.isdir(alluredir):
        return

    # workaround for allure to accept multiple devices as suites
    for json_file in Path(alluredir).glob("*.json"):
        json_data = json.loads(json_file.read_text())
        if "testCaseId" in json_data:
            json_data["parameters"] = [{"name": "device", "value": device}]
            json_data["testCaseId"] = device + json_data["testCaseId"]
            json_data["historyId"] = device + json_data["historyId"]
            json_file.write_text(json.dumps(json_data))

    allure_properties_file = Path(alluredir, "environment.properties")
    allure_properties_file.write_text(
        f"Version={results['tests/test_base.py::test_ubus_system_board']['version']}\n"
        f"Revision={results['tests/test_base.py::test_ubus_system_board']['revision']}\n"
    )


def ubus_call(command, namespace, method, params={}):
    output = command.run_check(f"ubus call {namespace} {method} '{json.dumps(params)}'")

    try:
        return json.loads("\n".join(output))
    except json.JSONDecodeError:
        return {}


@pytest.fixture(scope="session", autouse=True)
def setup_env(env, pytestconfig):
    env.config.data.setdefault("images", {})["firmware"] = pytestconfig.getoption(
        "firmware"
    )


@pytest.fixture
def shell_command(strategy):
    try:
        strategy.transition("shell")
        return strategy.shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)


@pytest.fixture
def ssh_command(shell_command, target):
    ssh = target.get_driver("SSHDriver")
    return ssh


def _host_ipv4_from_hostname_I() -> str:
    out = subprocess.check_output("hostname -I", shell=True, text=True).strip()
    if not out:
        raise RuntimeError("hostname -I returned nothing")
    # take the first token; if it's not IPv4, fall back to first IPv4 token
    first = out.split()[0]
    if ":" in first:
        first = next((t for t in out.split() if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", t)), "")
    if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", first or ""):
        raise RuntimeError(f"Could not determine IPv4 from: {out!r}")
    return first

@pytest.fixture
def upload_vwifi(shell_command,target):
    ssh = target.get_driver("SSHDriver")
    ssh.scp(src="vwifi/vwifi-client",dst=":/usr/bin/vwifi-client")
    path = "\n".join(ssh.run("which vwifi-client")[0])
    assert path == "/usr/bin/vwifi-client"

    # compute HOST IPv4 once (on the host)
    host_ip = _host_ipv4_from_hostname_I()
    host_ip_q = shlex.quote(host_ip)

    ssh.run_check("rmmod mac80211_hwsim")
    ssh.run_check("insmod mac80211_hwsim radios=0")
    cmd = f"""sh -lc '
        if command -v start-stop-daemon >/dev/null; then
          start-stop-daemon -S -b -m -p /tmp/vwifi.pid \
            -x /usr/bin/vwifi-client -- {host_ip_q} --number 2 \
            >/tmp/vwifi.log 2>&1
        else
          nohup /usr/bin/vwifi-client {host_ip_q} --number 2 \
            </dev/null >/tmp/vwifi.log 2>&1 & echo $! >/tmp/vwifi.pid
        fi
        '"""
    ssh.run_check(cmd)
    assert "\n".join(ssh.run("ps | grep vwifi")[0]) != ""
    time.sleep(5)
    ssh.run("wifi reload")
    ssh.run("wifi up")
    time.sleep(10)
    phy_devices = ssh.run("iw phy | grep phy")[0]
    assert len(phy_devices) == 4 #labgrid tokenize \t 
    iw_devices = "\n".join(ssh.run("iw dev")[0])                                      
    while "wlan0-mesh" not in iw_devices:                                                     
        iw_devices = "\n".join(ssh.run("iw dev")[0])                                  
        time.sleep(2)                                                                         
    stations = "\n".join(ssh.run("iw dev wlan0-mesh station dump")[0])                
    assert "02:00:00:00:00:01" in stations                                                    
    assert "02:00:00:00:00:02" in stations                                                    
    assert "02:00:00:00:00:03" in stations 
    return ssh

@pytest.fixture
def shell_command_fast(strategy):
    try:
        strategy.transition("shell")
        return strategy.shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)

@pytest.fixture
def shell_command_force_cycle(strategy):
    try:
        if hasattr(strategy, 'force_power_cycle'):
            strategy.force_power_cycle()
        else:
            strategy.transition("shell")
        return strategy.shell
    except Exception:
        logger.exception("Failed to transition to state shell")
        pytest.exit("Failed to transition to state shell", returncode=3)

# Fixture que apaga los routers al final (con env directo)
@pytest.fixture(scope="session", autouse=True)
def auto_router_lifecycle(env):
    """Apaga todos los targets del env al finalizar cada test (salvo KEEP_DUT_ON=1)."""
    yield

    if os.getenv("KEEP_DUT_ON", "0") == "1":
        logger.info("KEEP_DUT_ON=1, skipping power off for all targets")
        return

    for name, target in getattr(env, "targets", {}).items():
        try:
            # Prefer Strategy (aplica quirks); fallback a driver
            try:
                strat = target.get_driver("PhysicalDeviceStrategy")
                strat.transition("off")
                logger.info(f"[teardown] OFF via strategy: {name}")
            except Exception:
                power = target.get_driver("ExternalPowerDriver")
                target.activate(power)
                power.off()
                logger.info(f"[teardown] OFF via ExternalPowerDriver: {name}")
        except Exception as e:
            logger.warning(f"[teardown] Could not power off {name}: {e}")


@pytest.fixture
def belkin1_shell(env):
    """Shell del Belkin RT3200 #1."""
    target = env.get_target("belkin_rt3200_1")
    strategy = target.get_driver("PhysicalDeviceStrategy")
    strategy.transition("shell")
    return strategy.shell


@pytest.fixture
def belkin2_shell(env):
    """Shell del Belkin RT3200 #2."""
    target = env.get_target("belkin_rt3200_2")
    strategy = target.get_driver("PhysicalDeviceStrategy")
    strategy.transition("shell")
    return strategy.shell

@pytest.fixture
def glinet_shell(env):
    """Shell del GL-iNet MT300N-V2."""
    target = env.get_target("gl-mt300n-v2")
    strategy = target.get_driver("PhysicalDeviceStrategy")
    strategy.transition("shell")
    return strategy.shell


@pytest.fixture
def mesh_routers(belkin1_shell, belkin2_shell):
    """Fixture para tests que requieren múltiples routers."""
    return {
        "belkin1": belkin1_shell,
        "belkin2": belkin2_shell
    }


@pytest.fixture(scope="session")
def firmware_image(pytestconfig):
    """Obtiene la ruta de la imagen de firmware desde --firmware o variable de entorno."""
    firmware = pytestconfig.getoption("firmware")
    if not firmware or not os.path.exists(firmware):
        pytest.skip(f"Firmware image not found: {firmware}")
    return firmware


@pytest.fixture(scope="session", autouse=True)
def flash_firmware_if_requested(env, pytestconfig):
    """
    Automatically flashes firmware to all targets if --flash-firmware flag is provided.
    
    This fixture runs once per test session before any tests execute.
    
    Usage:
        pytest --lg-env targets/belkin_rt3200_1.yaml --firmware /path/to/image.bin --flash-firmware
    """
    if not pytestconfig.getoption("flash_firmware", default=False):
        return
    
    firmware = pytestconfig.getoption("firmware")
    if not firmware or not os.path.exists(firmware):
        pytest.fail(f"Cannot flash: firmware image not found: {firmware}")
    
    keep_config = pytestconfig.getoption("flash_keep_config", default=False)
    verify_version = pytestconfig.getoption("flash_verify_version", default=None)
    
    logger.info(f"Session firmware flash requested: {firmware} (keep_config={keep_config})")
    
    for name, target in env.targets.items():
        try:
            strategy = target.get_driver("PhysicalDeviceStrategy")
            logger.info(f"Flashing firmware on target '{name}'")
            strategy.provision_with_firmware(
                image_path=firmware,
                keep_config=keep_config,
                verify_version=verify_version
            )
            logger.info(f"Firmware flash completed successfully on target '{name}'")
        except Exception as e:
            logger.warning(f"Could not flash target '{name}': {e}")


@pytest.fixture
def flash_clean_firmware(strategy, firmware_image):
    """
    Flashes clean firmware (without preserving configuration) before a specific test.
    
    This fixture can be used by individual tests to ensure a known clean state.
    
    Usage:
        def test_fresh_install(flash_clean_firmware, ssh_command):
            # Router has clean firmware flashed
            ...
    """
    strategy.provision_with_firmware(firmware_image, keep_config=False)
    return strategy


@pytest.fixture
def sysupgrade_driver(target):
    """Provides direct access to the SysupgradeDriver for manual flashing operations."""
    return target.get_driver("SysupgradeDriver")
