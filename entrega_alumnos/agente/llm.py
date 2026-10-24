"""Cliente mínimo de Gemini: reintentos ante errores temporales y registro del consumo de tokens.

Lo usan el localizador y el reparador; cada etapa lleva su propio Consumo y lo devuelve
para que main.py sume el coste total en metricas.json.
"""
import json
import re
import time
from dataclasses import asdict, dataclass
from functools import cache

from google import genai
from google.genai import errors, types

MODELO = "gemini-3.5-flash-lite"
MAX_INTENTOS = 5
ESPERA_INICIAL = 2
CODIGOS_TEMPORALES = {429, 500, 503, 504}


@dataclass
class Consumo:
    llamadas: int = 0
    tokens_entrada: int = 0
    tokens_salida: int = 0

    def registrar(self, respuesta: types.GenerateContentResponse) -> None:
        uso = respuesta.usage_metadata
        self.llamadas += 1
        self.tokens_entrada += (uso and uso.prompt_token_count) or 0
        self.tokens_salida += (uso and uso.candidates_token_count) or 0

    def como_dict(self) -> dict:
        return asdict(self)


@cache
def _cliente() -> genai.Client:
    """Toma la clave de la variable de entorno GEMINI_API_KEY."""
    return genai.Client()


def _es_temporal(error: errors.APIError) -> bool:
    return error.code in CODIGOS_TEMPORALES


def _generar_con_reintentos(prompt: str, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    """Llama al modelo esperando cada vez el doble ante errores temporales (429, 503...)."""
    espera = ESPERA_INICIAL
    for intento in range(1, MAX_INTENTOS + 1):
        try:
            return _cliente().models.generate_content(model=MODELO, contents=prompt, config=config)
        except errors.APIError as error:
            if not _es_temporal(error) or intento == MAX_INTENTOS:
                raise
            time.sleep(espera)
            espera *= 2


def pedir_json(prompt: str, consumo: Consumo) -> dict:
    """Envía el prompt pidiendo una respuesta JSON y la devuelve ya decodificada."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json",
        temperature=0,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    respuesta = _generar_con_reintentos(prompt, config)
    consumo.registrar(respuesta)

    texto = respuesta.text or ""
    coincidencia = re.search(r"```(?:json)?\s*(.*?)\s*```", texto, re.DOTALL)
    texto_limpio = coincidencia.group(1) if coincidencia else texto.strip()
    texto_limpio = re.sub(r'\\(?!["\\/bfnrt]|u[0-9a-fA-F]{4})', r'\\\\', texto_limpio)

    try:
        return json.loads(texto_limpio)
    except json.JSONDecodeError:
        texto_limpio = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', texto_limpio)
        return json.loads(texto_limpio)