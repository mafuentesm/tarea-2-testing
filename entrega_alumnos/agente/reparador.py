"""REPARADOR: modifica el código fuente de repo/ para que los tests del bug pasen.

Uso por separado (con una localización ya guardada):
    python reparador.py <carpeta_del_bug> <localizacion.json>

Recibe:
  carpeta       Path de la carpeta del bug (contiene repo/, bug.json, run_test.sh, ...)
  info          contenido de bug.json como dict
  localizacion  lo que devolvió localizar() (ver localizador.py)

Hace: deja la reparación aplicada en carpeta/repo/. Solo puede cambiar código fuente, no tests.

Devuelve un dict con, al menos:
  {"resuelto": true}      # true si run_test.sh pasa al terminar
Puedes añadir más campos (p. ej. intentos o el coste en tokens).
"""
# import json
# import sys
# from pathlib import Path


# def reparar(carpeta: Path, info: dict, localizacion: dict) -> dict:
#     # TODO
#     raise NotImplementedError


# if __name__ == "__main__":
#     carpeta = Path(sys.argv[1]).resolve()
#     info = json.loads((carpeta / "bug.json").read_text())
#     localizacion = json.loads(Path(sys.argv[2]).read_text())
#     print(json.dumps(reparar(carpeta, info, localizacion), indent=2, ensure_ascii=False))

"""REPARADOR: modifica el código fuente de repo/ para que los tests del bug pasen.

Uso por separado (con una localización ya guardada):
    python reparador.py <carpeta_del_bug> <localizacion.json>
"""
import json
import sys
from pathlib import Path
import os
import signal
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET

from google.genai import errors

from codigo import codigo_numerado, es_archivo_de_test, leer_lineas
from llm import Consumo, pedir_json

SEGUNDOS_MAX_VERIFICACION = 40
SEGUNDOS_MAX_REGRESION = 90
MAX_REGRESIONES_EN_RETROALIMENTACION = 15
MAX_INTENTOS_POR_REGION = 2
MAX_CHARS_FEEDBACK = 3000

INSTRUCCIONES_REPARADOR = """Eres un experto en depuración y reparación automática de código Python.
Hay un bug en el archivo '{archivo}' dentro del rango de líneas {inicio} a {fin}.

## Contexto del problema
Análisis previo:
{analisis}

Tests que fallan originalmente:
{salida_tests}

{retroalimentacion}

## Código a reparar (líneas {inicio} a {fin} de {archivo})
{codigo_region}

## Instrucciones de Respuesta
Debes proponer la modificación exacta para reemplazar la región indicada.
Responde ÚNICAMENTE con un objeto JSON con este formato:
{{
  "analisis_arreglo": "breve explicación del cambio (1-2 frases)",
  "codigo_reparado": "código python completo que reemplazará exactamente las líneas {inicio} a {fin}"
}}
- El valor de "codigo_reparado" debe incluir la indentación correcta de las líneas a reemplazar.
- NUNCA modifiques tests, únicamente el código fuente de la región indicada.
"""


def reparar(carpeta: Path, info: dict, localizacion: dict, consumo: Consumo = None) -> dict:
    if consumo is None:
        consumo = Consumo()

    repo = carpeta / "repo"
    regiones = localizacion.get("regiones", [])
    salida_inicial = localizacion.get("salida_tests", "")
    analisis = localizacion.get("analisis", "")

    if not regiones:
        return {"resuelto": False, "coste": consumo.como_dict()}

    # Tests que pasaban con el código original: se miden una sola vez, cuando hagan falta
    pasaban = None
    linea_base_medida = False

    # Probar las regiones propuestas en orden de sospecha
    for reg in regiones:
        archivo_rel = reg.get("archivo")
        inicio = reg.get("inicio")
        fin = reg.get("fin")

        if not archivo_rel or inicio is None or fin is None:
            continue

        archivo_path = repo / archivo_rel
        if not archivo_path.is_file():
            continue

        texto_original = archivo_path.read_text(encoding="utf-8")
        lineas_originales = texto_original.splitlines(keepends=True) # Mantiene los \n
        if not (1 <= inicio <= fin <= len(lineas_originales)):
            continue

        codigo_region = codigo_numerado(lineas_originales, inicio, fin)
        retroalimentacion = ""

        for intento in range(MAX_INTENTOS_POR_REGION):
            prompt = INSTRUCCIONES_REPARADOR.format(
                archivo=archivo_rel,
                inicio=inicio,
                fin=fin,
                analisis=analisis,
                salida_tests=salida_inicial,
                retroalimentacion=f"## Retroalimentación de intento previo\n{retroalimentacion}" if retroalimentacion else "",
                codigo_region=codigo_region,
            )

            try:
                respuesta = pedir_json(prompt, consumo)
            except (errors.APIError, ValueError) as err:
                print(f"[reparador] error en llamada LLM: {err}", file=sys.stderr)
                continue

            if not isinstance(respuesta, dict) or "codigo_reparado" not in respuesta:
                continue

            codigo_reparado = respuesta["codigo_reparado"]

            # 1. Aplicar parche
            _aplicar_reparacion(archivo_path, lineas_originales, inicio, fin, codigo_reparado)

            # 2. Verificar tests objetivo (run_test.sh)
            pasa_objetivo, salida_obj = _ejecutar_run_test(carpeta)
            if not pasa_objetivo:
                _restaurar_archivo(archivo_path, lineas_originales)
                retroalimentacion = (
                    f"El intento {intento + 1} hizo que los tests del bug siguieran fallando:\n"
                    f"{salida_obj[-MAX_CHARS_FEEDBACK:]}"
                )
                continue

            # 3. Verificar regresiones: comparar contra los tests que pasaban con el código original
            if not linea_base_medida:
                pasaban = _medir_codigo_original(carpeta, info, archivo_path, lineas_originales)
                linea_base_medida = True

            sin_regresiones, mensaje_regresion = _verificar_regresiones(carpeta, info, pasaban)
            if not sin_regresiones:
                _restaurar_archivo(archivo_path, lineas_originales)
                retroalimentacion = (
                    f"El intento {intento + 1} hizo pasar el test objetivo pero introdujo REGRESIONES.\n"
                    f"{mensaje_regresion}"
                )
                continue

            # ¡Reparación exitosa sin regresiones!
            return {
                "resuelto": True,
                "region_aplicada": reg,
                "verificacion_regresion": mensaje_regresion,
                "coste": consumo.como_dict(),
            }

    return {"resuelto": False, "coste": consumo.como_dict()}


def _aplicar_reparacion(archivo_path: Path, lineas_originales: list[str], inicio: int, fin: int, codigo_nuevo: str) -> None:
    lineas_nuevas = codigo_nuevo.splitlines(keepends=True)
    if lineas_nuevas and not lineas_nuevas[-1].endswith("\n"):
        lineas_nuevas[-1] += "\n"

    prefijo = lineas_originales[: inicio - 1]
    sufijo = lineas_originales[fin:]
    contenido_final = "".join(prefijo + lineas_nuevas + sufijo)
    archivo_path.write_text(contenido_final, encoding="utf-8")


def _restaurar_archivo(archivo_path: Path, lineas_originales: list[str]) -> None:
    archivo_path.write_text("".join(lineas_originales), encoding="utf-8")


def _medir_codigo_original(carpeta: Path, info: dict, archivo_path: Path,
                           lineas_originales: list[str]) -> set[str] | None:
    """Tests que pasan con el código original: quita el parche un momento, mide y lo vuelve a poner."""
    # 1. Guardar el archivo con el parche y dejar el original
    contenido_parcheado = archivo_path.read_text(encoding="utf-8")
    _restaurar_archivo(archivo_path, lineas_originales)

    # 2. Medir; el finally garantiza que el parche vuelva aunque algo falle
    try:
        pasaban = _tests_que_pasan(carpeta, info)
    finally:
        # 3. Volver a poner el parche
        archivo_path.write_text(contenido_parcheado, encoding="utf-8")
    return pasaban


def _ejecutar_comando(cmd: list[str], cwd: Path, timeout: int = SEGUNDOS_MAX_VERIFICACION) -> tuple[bool, str]:
    proceso = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        salida, _ = proceso.communicate(timeout=timeout)
        exito = proceso.returncode == 0
    except subprocess.TimeoutExpired:
        os.killpg(proceso.pid, signal.SIGKILL)
        salida, _ = proceso.communicate()
        salida = (salida or "") + "\n[Timeout de verificación excedido]"
        exito = False
    return exito, salida


def _ejecutar_run_test(carpeta: Path) -> tuple[bool, str]:
    return _ejecutar_comando(["bash", str(carpeta / "run_test.sh")], cwd=carpeta)


def _restaurar_tests(repo: Path) -> None:
    """Deja los archivos de test como en el commit original, igual que run_test.sh."""
    resultado = subprocess.run(["git", "diff", "--name-only"], cwd=repo, capture_output=True, text=True)
    archivos_modificados = resultado.stdout.splitlines()

    tests_modificados = []
    for archivo in archivos_modificados:
        if es_archivo_de_test(archivo):
            tests_modificados.append(archivo)

    if tests_modificados:
        subprocess.run(["git", "checkout", "-q", "--", *tests_modificados], cwd=repo, capture_output=True)


def _rutas_para_pytest(modulos: list[str]) -> tuple[list[str], list[str]]:
    """Rutas y opciones para pytest. pytest no recolecta tests de un __init__.py, así que en ese
    caso se le pasa la carpeta y se le indica que también lea archivos __init__.py."""
    rutas = []
    hay_init = False
    for modulo in modulos:
        if modulo.endswith("__init__.py"):
            rutas.append(str(Path(modulo).parent))
            hay_init = True
        else:
            rutas.append(modulo)

    opciones = []
    if hay_init:
        opciones = ["-o", "python_files=__init__.py test_*.py *_test.py"]
    return rutas, opciones


def _comando_pytest(repo: Path, modulos: list[str], archivo_xml: Path, carpeta_cache: str) -> list[str]:
    """Comando que corre los módulos de test con el Python del bug y guarda el resultado en un XML."""
    rutas, opciones = _rutas_para_pytest(modulos)
    python_del_bug = str(repo / "env" / "bin" / "python")
    return [
        "env", f"XDG_CACHE_HOME={carpeta_cache}",     # caché limpia, como run_test.sh
        python_del_bug, "-m", "pytest",
        "-p", "no:cacheprovider",
        "-q",
        "--continue-on-collection-errors",
        f"--junitxml={archivo_xml}",
        *opciones,
        *rutas,
    ]


def _tests_que_pasaron(archivo_xml: Path) -> set[str] | None:
    """Nombres de los tests que pasaron según el XML de pytest; None si el XML no existe o está roto."""
    try:
        raiz = ET.parse(archivo_xml).getroot()
    except (ET.ParseError, FileNotFoundError):
        return None

    pasaron = set()
    for caso in raiz.iter("testcase"):
        resultados = [hijo.tag for hijo in caso]
        fallo = "failure" in resultados or "error" in resultados or "skipped" in resultados
        if not fallo:
            pasaron.add(f"{caso.get('classname')}::{caso.get('name')}")
    return pasaron


def _tests_que_pasan(carpeta: Path, info: dict) -> set[str] | None:
    """Tests de modulos_test que pasan con el código actual. None si no se pudo medir."""
    repo = carpeta / "repo"
    modulos = info.get("modulos_test", [])

    # 1. Los tests tienen que estar como en el original
    _restaurar_tests(repo)

    with tempfile.TemporaryDirectory() as carpeta_temporal:
        archivo_xml = Path(carpeta_temporal) / "resultado.xml"

        # 2. Correr pytest sobre los módulos de test
        comando = _comando_pytest(repo, modulos, archivo_xml, carpeta_temporal)
        _ejecutar_comando(comando, cwd=repo, timeout=SEGUNDOS_MAX_REGRESION)

        # 3. Leer qué tests pasaron (None si pytest no alcanzó a escribir el XML)
        return _tests_que_pasaron(archivo_xml)


def _verificar_regresiones(carpeta: Path, info: dict, pasaban: set[str] | None) -> tuple[bool, str]:
    """Compara los tests que pasan con el parche contra los que pasaban con el código original.
    Devuelve (sin_regresiones, mensaje para la retroalimentación)."""
    # 1. Si no hay con qué comparar, no se puede rechazar el parche
    if not info.get("modulos_test"):
        return True, "No hay modulos_test: regresión no verificada."
    if pasaban is None:
        return True, "No se pudo medir el código original: regresión no verificada."

    # 2. Medir con el parche aplicado
    pasan_ahora = _tests_que_pasan(carpeta, info)
    if pasan_ahora is None:
        return True, "La medición con el parche no terminó: regresión no verificada."

    # 3. Regresión = pasaba con el código original y ya no pasa
    regresiones = sorted(pasaban - pasan_ahora)
    if not regresiones:
        return True, "Sin regresiones."

    # 4. Armar el mensaje con los tests rotos
    lineas = []
    for test in regresiones[:MAX_REGRESIONES_EN_RETROALIMENTACION]:
        lineas.append(f"- {test}")
    lista = "\n".join(lineas)
    return False, f"Estos tests pasaban con el código original y ahora fallan:\n{lista}"


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    localizacion = json.loads(Path(sys.argv[2]).read_text())
    print(json.dumps(reparar(carpeta, info, localizacion), indent=2, ensure_ascii=False))