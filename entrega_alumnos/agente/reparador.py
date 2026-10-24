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
import time

from google.genai import errors

from codigo import codigo_numerado, leer_lineas
from llm import Consumo, pedir_json

SEGUNDOS_MAX_VERIFICACION = 40
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

            # 3. Verificar tests de regresión en modulos_test
            pasa_regresiones, salida_reg = _verificar_regresiones(carpeta, info)
            if not pasa_regresiones:
                _restaurar_archivo(archivo_path, lineas_originales)
                retroalimentacion = (
                    f"El intento {intento + 1} hizo pasar el test objetivo pero introdujo REGRESIONES:\n"
                    f"{salida_reg[-MAX_CHARS_FEEDBACK:]}"
                )
                continue

            # ¡Reparación exitosa sin regresiones!
            return {
                "resuelto": True,
                "region_aplicada": reg,
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


def _verificar_regresiones(carpeta: Path, info: dict) -> tuple[bool, str]:
    modulos = info.get("modulos_test", [])
    if not modulos:
        return True, ""

    repo = carpeta / "repo"
    env_bin = repo / "env" / "bin"
    pytest_bin = env_bin / "pytest"
    python_bin = env_bin / "python"

    salidas = []
    for mod in modulos:
        if pytest_bin.exists():
            cmd = [str(pytest_bin), mod]
        else:
            cmd = [str(python_bin), "-m", "unittest", mod]

        exito, salida = _ejecutar_comando(cmd, cwd=repo)
        if not exito:
            return False, f"Fallaron regresiones en módulo {mod}:\n{salida}"
        salidas.append(salida)

    return True, "\n".join(salidas)


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    localizacion = json.loads(Path(sys.argv[2]).read_text())
    print(json.dumps(reparar(carpeta, info, localizacion), indent=2, ensure_ascii=False))