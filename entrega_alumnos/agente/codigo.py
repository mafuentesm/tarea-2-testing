"""Utilidades para leer y analizar el código Python de repo/ sin ejecutarlo."""
import ast
import re
import subprocess
import textwrap
import warnings
from dataclasses import dataclass
from pathlib import Path

ES_TEST = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$")


@dataclass(frozen=True)
class Simbolo:
    """Función, método o clase de un archivo, con sus líneas (desde 1, ambas incluidas)."""
    nombre: str
    tipo: str
    inicio: int
    fin: int

    def contiene(self, linea: int) -> bool:
        return self.inicio <= linea <= self.fin

    @property
    def tamanio(self) -> int:
        return self.fin - self.inicio + 1


def es_archivo_de_test(ruta: str) -> bool:
    return bool(ES_TEST.search(ruta))


def archivos_fuente(repo: Path) -> set[str]:
    """Archivos .py versionados en el repo que no son de test (rutas relativas a repo/)."""
    salida = subprocess.run(["git", "ls-files", "*.py"], cwd=repo, capture_output=True, text=True).stdout
    return {ruta for ruta in salida.split() if not es_archivo_de_test(ruta)}


def leer_lineas(archivo: Path) -> list[str]:
    return archivo.read_text(errors="replace").splitlines()


def _parsear_codigo(codigo: str) -> ast.Module | None:
    """Árbol del código, o None si no es Python válido. Silencia los avisos de código antiguo."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(codigo)
    except (SyntaxError, ValueError):
        return None


def _parsear(archivo: Path) -> ast.Module | None:
    try:
        return _parsear_codigo(archivo.read_text(errors="replace"))
    except OSError:
        return None


def _inicio_con_decoradores(nodo: ast.AST) -> int:
    return min([nodo.lineno] + [decorador.lineno for decorador in getattr(nodo, "decorator_list", [])])


def _recorrer_simbolos(nodos: list[ast.stmt], prefijo: str) -> list[Simbolo]:
    simbolos = []
    for nodo in nodos:
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nombre = f"{prefijo}{nodo.name}"
            tipo = "class" if isinstance(nodo, ast.ClassDef) else "def"
            simbolos.append(Simbolo(nombre, tipo, _inicio_con_decoradores(nodo), nodo.end_lineno))
            simbolos += _recorrer_simbolos(nodo.body, f"{nombre}.")
    return simbolos


def simbolos_del_archivo(archivo: Path) -> list[Simbolo]:
    """Clases y funciones (incluidas las anidadas, con nombre cualificado) en orden de aparición."""
    arbol = _parsear(archivo)
    return _recorrer_simbolos(arbol.body, "") if arbol else []


def simbolo_mas_interno(simbolos: list[Simbolo], linea: int) -> Simbolo | None:
    """El símbolo más pequeño que contiene la línea (la función concreta antes que su clase)."""
    contenedores = [simbolo for simbolo in simbolos if simbolo.contiene(linea)]
    return min(contenedores, key=lambda simbolo: simbolo.tamanio, default=None)


def _nombre_llamado(llamada: ast.Call) -> str | None:
    """'f' en 'f(...)' y en 'objeto.f(...)'."""
    if isinstance(llamada.func, ast.Name):
        return llamada.func.id
    if isinstance(llamada.func, ast.Attribute):
        return llamada.func.attr
    return None


def _nombres_llamados(lineas: list[str], simbolo: Simbolo) -> set[str]:
    arbol = _parsear_codigo(textwrap.dedent("\n".join(lineas[simbolo.inicio - 1:simbolo.fin])))
    if not arbol:
        return set()
    return {nombre for nodo in ast.walk(arbol) if isinstance(nodo, ast.Call) and (nombre := _nombre_llamado(nodo))}


def _prefijo(simbolo: Simbolo) -> str:
    return simbolo.nombre.rpartition(".")[0]


def _definicion_mas_cercana(simbolos: list[Simbolo], nombre: str, desde: Simbolo) -> Simbolo | None:
    """La función del archivo con ese nombre, prefiriendo un método de la misma clase que `desde`."""
    candidatas = [s for s in simbolos if s.tipo == "def" and s.nombre.split(".")[-1] == nombre and s != desde]
    misma_clase = [s for s in candidatas if _prefijo(s) == _prefijo(desde)]
    return (misma_clase or candidatas or [None])[0]


def funciones_llamadas(lineas: list[str], simbolos: list[Simbolo], simbolo: Simbolo) -> list[Simbolo]:
    """Funciones definidas en el mismo archivo a las que llama el símbolo."""
    llamadas = (_definicion_mas_cercana(simbolos, nombre, simbolo) for nombre in _nombres_llamados(lineas, simbolo))
    return sorted({s for s in llamadas if s}, key=lambda s: s.inicio)


def encabezado_de_clase(simbolos: list[Simbolo], metodo: Simbolo) -> Simbolo | None:
    """Líneas de la clase de un método hasta su primer método: la declaración y sus atributos."""
    clase = next((s for s in simbolos if s.tipo == "class" and s.nombre == _prefijo(metodo)), None)
    if not clase:
        return None
    hijos = [s.inicio for s in simbolos if _prefijo(s) == clase.nombre]
    return Simbolo(clase.nombre, "class", clase.inicio, min(hijos, default=clase.fin + 1) - 1)


def codigo_numerado(lineas: list[str], inicio: int, fin: int) -> str:
    """Las líneas [inicio, fin] del archivo, cada una precedida de su número."""
    return "\n".join(f"{numero:>5} | {lineas[numero - 1]}" for numero in range(inicio, fin + 1))


def codigo_de_funcion(archivo: Path, nombre: str) -> str | None:
    """Código numerado de la primera función o método que se llame así en el archivo."""
    for simbolo in simbolos_del_archivo(archivo):
        if simbolo.tipo == "def" and simbolo.nombre.split(".")[-1] == nombre:
            return codigo_numerado(leer_lineas(archivo), simbolo.inicio, simbolo.fin)
    return None


def _rutas_posibles_de_modulo(modulo: str) -> list[str]:
    base = modulo.replace(".", "/")
    return [f"{base}.py", f"{base}/__init__.py"]


def _paquete_de(ruta: str) -> list[str]:
    """Paquete al que pertenece un archivo: tanto 'a/b/c.py' como 'a/b/__init__.py' -> ['a', 'b']."""
    return ruta.split("/")[:-1]


def _modulo_absoluto(nodo: ast.ImportFrom, paquete: list[str]) -> str | None:
    """Nombre absoluto del módulo de un 'from ... import', resolviendo los imports relativos."""
    if nodo.level == 0:
        return nodo.module
    subidas = nodo.level - 1
    if subidas > len(paquete):
        return None
    base = paquete[:len(paquete) - subidas] + ([nodo.module] if nodo.module else [])
    return ".".join(base) or None


def _modulos_importados(arbol: ast.Module, paquete: list[str]) -> dict[str, set[str]]:
    """Módulo absoluto importado -> nombres que se toman de él ('*' si es un import con asterisco)."""
    modulos: dict[str, set[str]] = {}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                modulos.setdefault(alias.name, set()).add((alias.asname or alias.name).split(".")[0])
        elif isinstance(nodo, ast.ImportFrom) and (modulo := _modulo_absoluto(nodo, paquete)):
            for alias in nodo.names:
                modulos.setdefault(modulo, set()).add(alias.name)
                modulos.setdefault(f"{modulo}.{alias.name}", set()).add(alias.asname or alias.name)
    return modulos


def importaciones_de_fuente(repo: Path, ruta: str, fuentes: set[str]) -> dict[str, set[str]]:
    """Archivos de código fuente del repo que importa el archivo -> nombres que importa de cada uno."""
    arbol = _parsear(repo / ruta)
    if not arbol:
        return {}
    importados: dict[str, set[str]] = {}
    for modulo, nombres in _modulos_importados(arbol, _paquete_de(ruta)).items():
        for destino in _rutas_posibles_de_modulo(modulo):
            if destino in fuentes and destino != ruta:
                importados.setdefault(destino, set()).update(nombres)
    return importados


def _reexportados(repo: Path, init: str, nombres: set[str], fuentes: set[str]) -> dict[str, set[str]]:
    """Submódulos de los que un __init__.py reexporta alguno de los nombres pedidos."""
    reexportados = {}
    for destino, exportados in importaciones_de_fuente(repo, init, fuentes).items():
        comunes = nombres if "*" in exportados else nombres & exportados
        if comunes:
            reexportados[destino] = set(comunes)
    return reexportados


def resolver_reexportaciones(repo: Path, importados: dict[str, set[str]], fuentes: set[str]) -> dict[str, set[str]]:
    """Cambia cada __init__.py por los submódulos donde están definidos los nombres que se le importan."""
    resueltos: dict[str, set[str]] = {}
    for ruta, nombres in importados.items():
        reexportados = _reexportados(repo, ruta, nombres, fuentes) if ruta.endswith("__init__.py") else {}
        for destino, comunes in (reexportados or {ruta: nombres}).items():
            resueltos.setdefault(destino, set()).update(comunes)
    return resueltos
