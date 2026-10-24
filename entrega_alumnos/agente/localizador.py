"""LOCALIZADOR: decide en qué parte del código fuente está el bug.

Uso por separado (para probarlo solo):
    python localizador.py <carpeta_del_bug>      # imprime la localización en JSON

Recibe:
  carpeta  Path de la carpeta del bug (contiene repo/, bug.json, run_test.sh, ...)
  info     contenido de bug.json como dict (id, comandos_test, archivos_test, modulos_test, ...)

Devuelve un dict con, al menos, este formato (se guarda como localizacion.json y se evalúa):
  {
    "regiones": [
      {"archivo": "black.py", "inicio": 120, "fin": 145}
    ]
  }
  - archivo: ruta relativa a repo/, de un archivo de código fuente (no de test)
  - inicio, fin: líneas (desde 1, ambas incluidas) del archivo con el bug
Puedes añadir más campos (p. ej. tu análisis o el coste en tokens) para usarlos en el reparador.

Estrategia (nada depende de un proyecto concreto):
  1. Ejecuta run_test.sh y extrae del traceback los frames que caen en código fuente del repo.
  2. Elige archivos candidatos: los del traceback y los que importan los tests y usa el test que falla.
  3. Le pasa al modelo la salida de los tests, el código de los tests y el de los candidatos
     (completo si es corto; si no, un índice de funciones, las funciones del traceback y, de las más
     profundas, los atributos de su clase y las funciones que llaman).
  4. El modelo responde qué funciones tienen el bug; sus líneas se calculan con ast.
     Si el modelo falla, se usa la función más profunda del traceback.
"""
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from google.genai import errors

from codigo import (Simbolo, archivos_fuente, codigo_de_funcion, codigo_numerado, encabezado_de_clase,
                    funciones_llamadas, importaciones_de_fuente, leer_lineas, resolver_reexportaciones,
                    simbolo_mas_interno, simbolos_del_archivo)
from llm import Consumo, pedir_json

SEGUNDOS_MAX_TESTS = 60
MAX_CHARS_SALIDA_TESTS = 8000
MAX_CHARS_CODIGO_TESTS = 8000
MAX_CHARS_CONTEXTO = 80000
MAX_ARCHIVOS_CANDIDATOS = 6
MAX_LINEAS_ARCHIVO_COMPLETO = 400
MAX_REGIONES = 3
NIVELES_DE_REEXPORTACION = 2
FRAMES_CON_LLAMADAS = 2

# Frames de traceback: estilo Python ('File "x.py", line 12') y estilo pytest ('x.py:12: in f').
FRAME_PYTHON = re.compile(r'File "([^"]+\.py)", line (\d+)')
FRAME_PYTEST = re.compile(r"^([^\s:]+\.py):(\d+):", re.MULTILINE)
NOMBRE_DE_TEST = re.compile(r"(?:::|\.)(\w+)(?:\[.*\])?\s*$")

INSTRUCCIONES = """Eres un experto en depuración de proyectos Python. Unos tests fallan por un bug en el
código fuente (no en los tests). Con la información de abajo, decide qué funciones del código fuente hay
que modificar para arreglar el bug. Los tests son correctos: describen el comportamiento esperado.
La línea donde salta la excepción no siempre es la del bug: revisa también el código que deja el estado
incorrecto (las funciones que se llaman antes y los atributos que modifican).

Responde SOLO con un JSON con este formato:
{{
  "analisis": "causa del bug en 2-4 frases y qué habría que cambiar",
  "regiones": [
    {{"archivo": "ruta/relativa.py", "simbolo": "Clase.metodo", "inicio": 10, "fin": 25}}
  ]
}}
- "archivo": uno de los archivos de código fuente mostrados (nunca un archivo de test).
- "simbolo": nombre cualificado de la función o método tal como aparece en el índice; "" si el
  código a cambiar está fuera de toda función.
- "inicio", "fin": líneas del archivo donde está el bug.
- Ordena las regiones de más a menos probable; como máximo {max_regiones}.

## Tests que fallan
{comandos}

## Salida de los tests
{salida}

## Código de los tests que fallan
{codigo_tests}

## Código fuente candidato
{contexto}
"""


def localizar(carpeta: Path, info: dict) -> dict:
    repo = carpeta / "repo"
    fuentes = archivos_fuente(repo)
    salida_tests = ejecutar_tests(carpeta)
    frames = frames_de_fuente(salida_tests, repo, fuentes)
    codigo_tests = codigo_de_los_tests(repo, info)
    candidatos = archivos_candidatos(repo, info, frames, fuentes, codigo_tests)

    consumo = Consumo()
    prompt = construir_prompt(info, salida_tests, codigo_tests, contexto_de_candidatos(repo, candidatos, frames))
    respuesta = consultar_modelo(prompt, consumo)
    regiones = regiones_de_respuesta(respuesta, repo, fuentes) or regiones_por_defecto(repo, frames, candidatos)

    return {
        "regiones": regiones,
        "analisis": respuesta.get("analisis", ""),
        "salida_tests": salida_tests,
        "coste": consumo.como_dict(),
    }


# ---------- 1. Ejecución de los tests y traceback ----------

def ejecutar_tests(carpeta: Path) -> str:
    """Salida de run_test.sh (el final, que es donde están los tracebacks). Si se cuelga, se corta."""
    proceso = subprocess.Popen(["bash", str(carpeta / "run_test.sh")], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
    try:
        salida, _ = proceso.communicate(timeout=SEGUNDOS_MAX_TESTS)
    except subprocess.TimeoutExpired:
        os.killpg(proceso.pid, signal.SIGKILL)
        salida = (proceso.communicate()[0] or "") + "\n[tests detenidos por tiempo]"
    return salida[-MAX_CHARS_SALIDA_TESTS:]


def _ruta_relativa_al_repo(ruta: str, repo: Path) -> str | None:
    absoluta = Path(ruta) if Path(ruta).is_absolute() else repo / ruta
    try:
        return absoluta.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None


def frames_de_fuente(salida_tests: str, repo: Path, fuentes: set[str]) -> list[tuple[str, int]]:
    """(archivo, línea) de los frames del traceback en código fuente, del más profundo al menos."""
    coincidencias = sorted([*FRAME_PYTHON.finditer(salida_tests), *FRAME_PYTEST.finditer(salida_tests)],
                           key=lambda coincidencia: coincidencia.start())
    frames = []
    for ruta, linea in (coincidencia.groups() for coincidencia in coincidencias):
        relativa = _ruta_relativa_al_repo(ruta, repo)
        if relativa in fuentes and (relativa, int(linea)) not in frames:
            frames.append((relativa, int(linea)))
    return list(reversed(frames))


# ---------- 2. Código de los tests y archivos candidatos ----------

def nombres_de_tests(info: dict) -> list[str]:
    """Nombre de la función de test de cada comando (unittest 'a.b.Clase.test_x' o pytest 'f.py::test_x')."""
    nombres = []
    for comando in info["comandos_test"]:
        coincidencia = NOMBRE_DE_TEST.search(comando)
        if coincidencia and coincidencia.group(1) not in nombres:
            nombres.append(coincidencia.group(1))
    return nombres


def codigo_de_los_tests(repo: Path, info: dict) -> str:
    """Código numerado de las funciones de test que fallan, buscadas en los archivos de test del bug."""
    fragmentos = []
    for nombre in nombres_de_tests(info):
        for archivo in info["archivos_test"]:
            codigo = codigo_de_funcion(repo / archivo, nombre) if (repo / archivo).is_file() else None
            if codigo:
                fragmentos.append(f"### {archivo} :: {nombre}\n{codigo}")
                break
    return "\n\n".join(fragmentos)[:MAX_CHARS_CODIGO_TESTS] or "(no encontrado)"


def _importados_por_los_tests(repo: Path, info: dict, fuentes: set[str]) -> dict[str, set[str]]:
    """Archivos fuente que importan los tests -> nombres importados, atravesando los __init__.py."""
    importados: dict[str, set[str]] = {}
    for archivo in info["archivos_test"]:
        for ruta, nombres in importaciones_de_fuente(repo, archivo, fuentes).items():
            importados.setdefault(ruta, set()).update(nombres)
    for _ in range(NIVELES_DE_REEXPORTACION):
        importados = resolver_reexportaciones(repo, importados, fuentes)
    return importados


def _veces_usados(nombres: set[str], codigo: str) -> int:
    return sum(len(re.findall(rf"\b{re.escape(nombre)}\b", codigo)) for nombre in nombres)


def _texto_de_archivos_test(repo: Path, info: dict) -> str:
    return "\n".join((repo / archivo).read_text(errors="replace") for archivo in info["archivos_test"]
                     if (repo / archivo).is_file())


def archivos_candidatos(repo: Path, info: dict, frames: list[tuple[str, int]], fuentes: set[str],
                        codigo_tests: str) -> list[str]:
    """Archivos del traceback primero; después los importados por los tests, según cuánto los usa el test
    que falla (y, a igualdad, el archivo de test completo)."""
    del_traceback = [archivo for archivo, _ in frames]
    importados = _importados_por_los_tests(repo, info, fuentes)
    archivos_test = _texto_de_archivos_test(repo, info)
    por_uso = sorted(importados, reverse=True, key=lambda ruta: (_veces_usados(importados[ruta], codigo_tests),
                                                                 _veces_usados(importados[ruta], archivos_test)))
    sin_repetir = list(dict.fromkeys(del_traceback + por_uso))
    return sin_repetir[:MAX_ARCHIVOS_CANDIDATOS]


# ---------- 3. Contexto y prompt ----------

def _indice_de_simbolos(simbolos: list[Simbolo]) -> str:
    return "\n".join(f"  L{s.inicio}-{s.fin} {s.tipo} {s.nombre}" for s in simbolos)


def _con_su_contexto(lineas: list[str], simbolos: list[Simbolo], funcion: Simbolo) -> list[Simbolo | None]:
    """La función precedida de los atributos de su clase y seguida de las funciones que llama."""
    return [encabezado_de_clase(simbolos, funcion), funcion, *funciones_llamadas(lineas, simbolos, funcion)]


def _funciones_del_traceback(lineas: list[str], simbolos: list[Simbolo], lineas_frames: list[int]) -> str:
    """Código de las funciones de los frames; las más profundas, con su clase y las funciones que llaman."""
    funciones = [f for f in dict.fromkeys(simbolo_mas_interno(simbolos, linea) for linea in lineas_frames) if f]
    profundas = [s for f in funciones[:FRAMES_CON_LLAMADAS] for s in _con_su_contexto(lineas, simbolos, f)]
    bloques = dict.fromkeys(s for s in profundas + funciones[FRAMES_CON_LLAMADAS:] if s)
    return "\n\n".join(codigo_numerado(lineas, s.inicio, s.fin) for s in bloques)


def contexto_de_archivo(repo: Path, archivo: str, lineas_frames: list[int]) -> str:
    """Archivo completo si es corto; si no, su índice de funciones y las funciones que aparecen en el traceback."""
    lineas = leer_lineas(repo / archivo)
    if len(lineas) <= MAX_LINEAS_ARCHIVO_COMPLETO:
        return f"### {archivo} (completo)\n{codigo_numerado(lineas, 1, len(lineas))}"
    simbolos = simbolos_del_archivo(repo / archivo)
    contexto = f"### {archivo} ({len(lineas)} líneas) — índice de funciones\n{_indice_de_simbolos(simbolos)}"
    funciones = _funciones_del_traceback(lineas, simbolos, lineas_frames)
    return f"{contexto}\n\n### {archivo} — funciones del traceback y las que llaman\n{funciones}" if funciones else contexto


def contexto_de_candidatos(repo: Path, candidatos: list[str], frames: list[tuple[str, int]]) -> str:
    """Contexto de cada candidato, en orden de prioridad, sin pasar del presupuesto de caracteres."""
    partes, total = [], 0
    for archivo in candidatos:
        parte = contexto_de_archivo(repo, archivo, [linea for ruta, linea in frames if ruta == archivo])
        if partes and total + len(parte) > MAX_CHARS_CONTEXTO:
            continue
        partes.append(parte)
        total += len(parte)
    return "\n\n".join(partes)[:MAX_CHARS_CONTEXTO] or "(no se encontraron archivos candidatos)"


def construir_prompt(info: dict, salida_tests: str, codigo_tests: str, contexto: str) -> str:
    return INSTRUCCIONES.format(max_regiones=MAX_REGIONES, comandos="\n".join(info["comandos_test"]),
                                salida=salida_tests, codigo_tests=codigo_tests, contexto=contexto)


def consultar_modelo(prompt: str, consumo: Consumo) -> dict:
    """Respuesta JSON del modelo, o {} si no responde o lo que responde no es un objeto JSON."""
    try:
        respuesta = pedir_json(prompt, consumo)
    except (errors.APIError, ValueError) as error:
        print(f"[localizador] el modelo no dio una respuesta válida: {error}", file=sys.stderr)
        return {}
    return respuesta if isinstance(respuesta, dict) else {}


# ---------- 4. Regiones ----------

def region(archivo: str, inicio: int, fin: int) -> dict:
    return {"archivo": archivo, "inicio": inicio, "fin": fin}


def _lineas_validas(inicio, fin, total_lineas: int) -> bool:
    return isinstance(inicio, int) and isinstance(fin, int) and 1 <= inicio <= fin <= total_lineas


def _simbolo_por_nombre(simbolos: list[Simbolo], nombre: str) -> Simbolo | None:
    exacto = [s for s in simbolos if s.nombre == nombre]
    por_sufijo = [s for s in simbolos if s.nombre.split(".")[-1] == nombre.split(".")[-1]]
    return (exacto or por_sufijo or [None])[0]


def region_propuesta(propuesta: dict, repo: Path, fuentes: set[str]) -> dict | None:
    """Convierte una región del modelo en una válida: si cae en un símbolo, expande a la función completa."""
    archivo = propuesta.get("archivo")
    if archivo not in fuentes:
        return None
    
    simbolos = simbolos_del_archivo(repo / archivo)
    simbolo = _simbolo_por_nombre(simbolos, str(propuesta.get("simbolo") or ""))
    
    # Si el modelo no dio símbolo exacto, buscar si el inicio cae dentro de alguna función
    inicio_prop = propuesta.get("inicio")
    if not simbolo and isinstance(inicio_prop, int):
        simbolo = simbolo_mas_interno(simbolos, inicio_prop)

    if simbolo:
        return region(archivo, simbolo.inicio, simbolo.fin)

    fin_prop = propuesta.get("fin")
    return region(archivo, inicio_prop, fin_prop) if _lineas_validas(inicio_prop, fin_prop, len(leer_lineas(repo / archivo))) else None


def regiones_de_respuesta(respuesta: dict, repo: Path, fuentes: set[str]) -> list[dict]:
    propuestas = respuesta.get("regiones")
    if not isinstance(propuestas, list):
        return []
    regiones = [region_propuesta(p, repo, fuentes) for p in propuestas if isinstance(p, dict)]
    return list({json.dumps(r): r for r in regiones if r}.values())[:MAX_REGIONES]


def regiones_por_defecto(repo: Path, frames: list[tuple[str, int]], candidatos: list[str]) -> list[dict]:
    """Sin respuesta útil del modelo: la función del frame más profundo o, si no hay, el primer candidato."""
    for archivo, linea in frames:
        simbolo = simbolo_mas_interno(simbolos_del_archivo(repo / archivo), linea)
        if simbolo:
            return [region(archivo, simbolo.inicio, simbolo.fin)]
    if frames:
        archivo, linea = frames[0]
        return [region(archivo, linea, linea)]
    if candidatos:
        return [region(candidatos[0], 1, max(1, len(leer_lineas(repo / candidatos[0]))))]
    return []


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    print(json.dumps(localizar(carpeta, info), indent=2, ensure_ascii=False))
