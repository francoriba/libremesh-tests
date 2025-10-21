import pytest
import allure
import time

@pytest.mark.lg_single_router
@pytest.mark.lg_smoke
def test_single_router_basic(belkin1_shell):
    """Test básico en un solo router."""
    
    with allure.step("Verificar que el router responde"):
        result = belkin1_shell.run("uname -a", timeout=10)
        assert result[2] == 0
        assert "Linux" in result[0][0]
    
    with allure.step("Verificar conectividad de red"):
        result = belkin1_shell.run("ip addr show", timeout=10)
        assert result[2] == 0


@pytest.mark.lg_multi_router
@pytest.mark.lg_mesh
def test_mesh_basic_connectivity(mesh_routers):
    """Test básico de conectividad mesh entre tres routers (2 Belkin + 1 GL-iNet)."""
    
    belkin1 = mesh_routers["belkin1"]
    belkin2 = mesh_routers["belkin2"]
    glinet = mesh_routers["glinet"]
    
    with allure.step("Verificar que los 3 routers están activos"):
        result1 = belkin1.run("echo 'belkin1_active'", timeout=5)
        result2 = belkin2.run("echo 'belkin2_active'", timeout=5)
        result3 = glinet.run("echo 'glinet_active'", timeout=5)
        
        assert result1[2] == 0
        assert result2[2] == 0
        assert result3[2] == 0
        assert "belkin1_active" in result1[0][0]
        assert "belkin2_active" in result2[0][0]
        assert "glinet_active" in result3[0][0]
    
    with allure.step("Esperar convergencia de la red mesh batman-adv"):
        """
        LibreMesh usa batman-adv para mesh networking.
        Necesitamos esperar a que los nodos se descubran y establezcan rutas.
        """
        def wait_for_mesh_convergence(routers_dict, max_wait=120, poll_interval=10):
            """Espera hasta que todos los routers tengan vecinos batman activos."""
            start_time = time.time()
            
            print("\n⏳ Esperando convergencia de batman-adv mesh network...")
            
            while (time.time() - start_time) < max_wait:
                all_ready = True
                status = {}
                
                for name, router in routers_dict.items():
                    # Contar vecinos batman activos (líneas con 'LiMe_' y 'mesh')
                    result = router.run("batctl n 2>/dev/null | grep -c 'LiMe_' || echo 0", timeout=10)
                    try:
                        neighbor_count = int(result[0][0].strip()) if result[2] == 0 and result[0] else 0
                    except (ValueError, IndexError):
                        neighbor_count = 0
                    
                    status[name] = neighbor_count
                    
                    # Cada router debería ver al menos 1 vecino para formar la malla
                    if neighbor_count < 1:
                        all_ready = False
                
                elapsed = int(time.time() - start_time)
                status_str = ", ".join([f"{k}={v} vecinos" for k, v in status.items()])
                print(f"  [{elapsed}s] Estado: {status_str}")
                
                if all_ready:
                    print(f"✅ Mesh convergida en {elapsed} segundos - Todos los nodos tienen vecinos")
                    return True
                
                if (time.time() - start_time) < max_wait:
                    time.sleep(poll_interval)
            
            print(f"⚠️  Timeout después de {max_wait}s esperando convergencia mesh.")
            print(f"    Estado final: {status_str}")
            return False
        
        routers = {"Belkin1": belkin1, "Belkin2": belkin2, "GLiNet": glinet}
        mesh_ready = wait_for_mesh_convergence(routers, max_wait=120, poll_interval=10)
        
        # Advertencia si no convergió, pero continuar para obtener más información de debug
        if not mesh_ready:
            print("⚠️  Continuando el test para obtener información de debug...")
    
    with allure.step("Obtener direcciones IP de los 3 routers"):
        # Obtener IP del Belkin 1
        ip1_result = belkin1.run("ip route get 1.1.1.1 | head -1 | awk '{print $7}'", timeout=10)
        assert ip1_result[2] == 0
        belkin1_ip = ip1_result[0][0].strip()
        
        # Obtener IP del Belkin 2
        ip2_result = belkin2.run("ip route get 1.1.1.1 | head -1 | awk '{print $7}'", timeout=10)
        assert ip2_result[2] == 0
        belkin2_ip = ip2_result[0][0].strip()
        
        # Obtener IP del GL-iNet
        ip3_result = glinet.run("ip route get 1.1.1.1 | head -1 | awk '{print $7}'", timeout=10)
        assert ip3_result[2] == 0
        glinet_ip = ip3_result[0][0].strip()
        
        # Verificar que todas las IPs son diferentes
        assert belkin1_ip != belkin2_ip, "Belkin 1 y Belkin 2 deben tener IPs diferentes"
        assert belkin1_ip != glinet_ip, "Belkin 1 y GL-iNet deben tener IPs diferentes"
        assert belkin2_ip != glinet_ip, "Belkin 2 y GL-iNet deben tener IPs diferentes"
        
        print(f"IPs detectadas: Belkin1={belkin1_ip}, Belkin2={belkin2_ip}, GLiNet={glinet_ip}")
    
    with allure.step("Test de conectividad: Belkin 1 ↔ Belkin 2"):
        # Belkin 1 → Belkin 2
        ping_result = belkin1.run(f"ping -c 3 -W 5 {belkin2_ip}", timeout=20)
        assert ping_result[2] == 0, f"Ping Belkin1→Belkin2 falló: {ping_result[1]}"
        assert "0% packet loss" in " ".join(ping_result[0])
        
        # Belkin 2 → Belkin 1
        ping_result = belkin2.run(f"ping -c 3 -W 5 {belkin1_ip}", timeout=20)
        assert ping_result[2] == 0, f"Ping Belkin2→Belkin1 falló: {ping_result[1]}"
        assert "0% packet loss" in " ".join(ping_result[0])
    
    with allure.step("Test de conectividad: Belkin 1 ↔ GL-iNet"):
        # Belkin 1 → GL-iNet
        ping_result = belkin1.run(f"ping -c 3 -W 5 {glinet_ip}", timeout=20)
        assert ping_result[2] == 0, f"Ping Belkin1→GLiNet falló: {ping_result[1]}"
        assert "0% packet loss" in " ".join(ping_result[0])
        
        # GL-iNet → Belkin 1
        ping_result = glinet.run(f"ping -c 3 -W 5 {belkin1_ip}", timeout=20)
        assert ping_result[2] == 0, f"Ping GLiNet→Belkin1 falló: {ping_result[1]}"
        assert "0% packet loss" in " ".join(ping_result[0])
    
    with allure.step("Test de conectividad: Belkin 2 ↔ GL-iNet"):
        # Belkin 2 → GL-iNet
        ping_result = belkin2.run(f"ping -c 3 -W 5 {glinet_ip}", timeout=20)
        assert ping_result[2] == 0, f"Ping Belkin2→GLiNet falló: {ping_result[1]}"
        assert "0% packet loss" in " ".join(ping_result[0])
        
        # GL-iNet → Belkin 2
        ping_result = glinet.run(f"ping -c 3 -W 5 {belkin2_ip}", timeout=20)
        assert ping_result[2] == 0, f"Ping GLiNet→Belkin2 falló: {ping_result[1]}"
        assert "0% packet loss" in " ".join(ping_result[0])


@pytest.mark.lg_multi_router
@pytest.mark.lg_mesh
def test_mesh_batman_connectivity(mesh_routers):
    """Test de conectividad mesh usando batman-adv (batctl) con 3 routers."""
    
    belkin1 = mesh_routers["belkin1"]
    belkin2 = mesh_routers["belkin2"]
    glinet = mesh_routers["glinet"]
    
    with allure.step("Verificar que batman-adv está funcionando en los 3 routers"):
        # Verificar que batctl funciona en Belkin 1
        batman1_result = belkin1.run("batctl n", timeout=10)
        assert batman1_result[2] == 0, "batctl no funciona en Belkin 1"
        
        # Verificar que batctl funciona en Belkin 2
        batman2_result = belkin2.run("batctl n", timeout=10)
        assert batman2_result[2] == 0, "batctl no funciona en Belkin 2"
        
        # Verificar que batctl funciona en GL-iNet
        batman3_result = glinet.run("batctl n", timeout=10)
        assert batman3_result[2] == 0, "batctl no funciona en GL-iNet"
    
    with allure.step("Obtener información de interfaces batman-adv"):
        # Comando más simple para obtener interfaces batman
        bat1_if_result = belkin1.run("batctl if", timeout=10)
        assert bat1_if_result[2] == 0, "No se pudo obtener interfaces batman de Belkin 1"
        
        bat2_if_result = belkin2.run("batctl if", timeout=10)
        assert bat2_if_result[2] == 0, "No se pudo obtener interfaces batman de Belkin 2"
        
        bat3_if_result = glinet.run("batctl if", timeout=10)
        assert bat3_if_result[2] == 0, "No se pudo obtener interfaces batman de GL-iNet"
        
        # Verificar que los 3 routers tienen interfaces batman configuradas
        bat1_output = " ".join(bat1_if_result[0]) if bat1_if_result[0] else ""
        bat2_output = " ".join(bat2_if_result[0]) if bat2_if_result[0] else ""
        bat3_output = " ".join(bat3_if_result[0]) if bat3_if_result[0] else ""
        
        assert "active" in bat1_output.lower(), \
            f"Belkin 1 no tiene interfaces batman activas: {bat1_output}"
        assert "active" in bat2_output.lower(), \
            f"Belkin 2 no tiene interfaces batman activas: {bat2_output}"
        assert "active" in bat3_output.lower(), \
            f"GL-iNet no tiene interfaces batman activas: {bat3_output}"
    
    with allure.step("Verificar vecinos batman en los 3 routers"):
        # Obtener outputs de vecinos
        neighbors1_output = " ".join(batman1_result[0]) if batman1_result[0] else ""
        neighbors2_output = " ".join(batman2_result[0]) if batman2_result[0] else ""
        neighbors3_output = " ".join(batman3_result[0]) if batman3_result[0] else ""
        
        print(f"\n=== Vecinos Belkin 1 ===\n{neighbors1_output[:500]}")
        print(f"\n=== Vecinos Belkin 2 ===\n{neighbors2_output[:500]}")
        print(f"\n=== Vecinos GL-iNet ===\n{neighbors3_output[:500]}")
    
    with allure.step("Verificar que los routers se ven como vecinos batman"):
        # Verificar que cada router tiene vecinos
        assert "last-seen" in neighbors1_output.lower(), \
            f"Belkin 1 no muestra vecinos batman: {neighbors1_output[:200]}"
        assert "last-seen" in neighbors2_output.lower(), \
            f"Belkin 2 no muestra vecinos batman: {neighbors2_output[:200]}"
        assert "last-seen" in neighbors3_output.lower(), \
            f"GL-iNet no muestra vecinos batman: {neighbors3_output[:200]}"
        
        # Verificar que hay al menos entradas de vecinos (no solo header)
        neighbors1_lines = [line.strip() for line in batman1_result[0] if line.strip()]
        neighbors2_lines = [line.strip() for line in batman2_result[0] if line.strip()]
        neighbors3_lines = [line.strip() for line in batman3_result[0] if line.strip()]
        
        assert len(neighbors1_lines) >= 3, f"Belkin 1 no tiene suficientes líneas de vecinos: {len(neighbors1_lines)}"
        assert len(neighbors2_lines) >= 3, f"Belkin 2 no tiene suficientes líneas de vecinos: {len(neighbors2_lines)}"
        assert len(neighbors3_lines) >= 3, f"GL-iNet no tiene suficientes líneas de vecinos: {len(neighbors3_lines)}"
        
        # Verificar que cada router ve vecinos LiMe
        router1_sees_lime = any("LiMe_" in line for line in batman1_result[0])
        router2_sees_lime = any("LiMe_" in line for line in batman2_result[0])
        router3_sees_lime = any("LiMe_" in line for line in batman3_result[0])
        
        assert router1_sees_lime, "Belkin 1 no ve vecinos LiMe en su output"
        assert router2_sees_lime, "Belkin 2 no ve vecinos LiMe en su output"
        assert router3_sees_lime, "GL-iNet no ve vecinos LiMe en su output"
    
    with allure.step("Verificar calidad de enlace mesh y contar vecinos activos"):
        def count_active_neighbors(batman_result, router_name):
            """Cuenta vecinos activos en el output de batctl n."""
            active_count = 0
            for line in batman_result[0]:
                # Buscar líneas como "   wlan0-mesh_29      LiMe_f89fab_wlan0_mesh_29    4.360s"
                if "mesh" in line and "LiMe_" in line and line.strip():
                    # Verificar si la línea termina con un tiempo válido
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        last_part = parts[-1]
                        if last_part.endswith('s') and last_part != "last-seen":
                            try:
                                # Extraer número del tiempo (ej: "4.360s" -> 4.360)
                                time_str = last_part[:-1]  # Quitar la 's'
                                seconds = float(time_str)
                                if 0 < seconds < 30:  # Tiempo razonable
                                    active_count += 1
                            except ValueError:
                                pass  # No es un tiempo válido, continuar
            return active_count
        
        # Contar vecinos activos en cada router
        active_neighbors_1 = count_active_neighbors(batman1_result, "Belkin 1")
        active_neighbors_2 = count_active_neighbors(batman2_result, "Belkin 2")
        active_neighbors_3 = count_active_neighbors(batman3_result, "GL-iNet")
        
        # Verificar que cada router tiene al menos un vecino activo
        # En una red mesh de 3 nodos, cada nodo debería ver al menos 1 vecino (idealmente 2)
        assert active_neighbors_1 > 0, \
            f"Belkin 1 no tiene vecinos batman activos (encontrados: {active_neighbors_1}). " \
            f"Líneas de batctl: {[line for line in batman1_result[0] if 'mesh' in line or 'LiMe_' in line][:5]}"
        
        assert active_neighbors_2 > 0, \
            f"Belkin 2 no tiene vecinos batman activos (encontrados: {active_neighbors_2}). " \
            f"Líneas de batctl: {[line for line in batman2_result[0] if 'mesh' in line or 'LiMe_' in line][:5]}"
        
        assert active_neighbors_3 > 0, \
            f"GL-iNet no tiene vecinos batman activos (encontrados: {active_neighbors_3}). " \
            f"Líneas de batctl: {[line for line in batman3_result[0] if 'mesh' in line or 'LiMe_' in line][:5]}"
        
        # Log para debugging
        print(f"\n✅ Belkin 1 tiene {active_neighbors_1} vecinos activos")
        print(f"✅ Belkin 2 tiene {active_neighbors_2} vecinos activos")
        print(f"✅ GL-iNet tiene {active_neighbors_3} vecinos activos")
        print(f"✅ Total de enlaces en la malla: {(active_neighbors_1 + active_neighbors_2 + active_neighbors_3) // 2}")


@pytest.mark.lg_multi_router
@pytest.mark.lg_mesh
@pytest.mark.lg_keep_routers_on(["belkin_rt3200_1"])  # Solo mantener Belkin 1 encendido para debugging
def test_mesh_advanced_debugging(mesh_routers):
    """Test avanzado con debugging - mantiene Belkin 1 encendido al finalizar."""
    
    belkin1 = mesh_routers["belkin1"]
    belkin2 = mesh_routers["belkin2"]
    glinet = mesh_routers["glinet"]
    
    with allure.step("Verificar configuración de mesh en los 3 routers"):
        # Verificar que LibreMesh está configurado en todos
        lime_config1 = belkin1.run("lime-config show network", timeout=15)
        lime_config2 = belkin2.run("lime-config show network", timeout=15)
        lime_config3 = glinet.run("lime-config show network", timeout=15)
        
        assert lime_config1[2] == 0, "LibreMesh no configurado en Belkin 1"
        assert lime_config2[2] == 0, "LibreMesh no configurado en Belkin 2"
        assert lime_config3[2] == 0, "LibreMesh no configurado en GL-iNet"
    
    # Al final: belkin_rt3200_1 queda encendido, belkin_rt3200_2 y gl_mt300n_v2 se apagan


@pytest.mark.lg_routers(["belkin_rt3200_1", "belkin_rt3200_2", "gl_mt300n_v2"])  # Especificación explícita de los 3
def test_mesh_with_explicit_routers(shell_command):
    """Test que usa shell_command genérico pero especifica los 3 routers explícitamente."""
    
    # Este test usa el shell_command del router principal (definido por YAML)
    # pero el marker lg_routers especifica que también usa los otros 2 routers
    
    result = shell_command.run("echo 'test with 3 routers'", timeout=5)
    assert result[2] == 0
    
    # Al final: los 3 routers se apagan
