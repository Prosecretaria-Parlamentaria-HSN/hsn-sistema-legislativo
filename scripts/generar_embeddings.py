#!/usr/bin/env python3
"""
generar_embeddings.py — Índice de "búsqueda por significado" (RAG, etapa R)
===========================================================================
Convierte el texto de cada proyecto de data/proyectos.json en un vector
(embedding) con el modelo multilingüe `intfloat/multilingual-e5-small` y lo
guarda en:

  data/embeddings_proyectos.json   metadatos: modelo, dimensión, claves, hashes
  data/embeddings_proyectos.bin    matriz int8 (N x dim), vectores normalizados * 127

La web (en el navegador) carga estos dos archivos y compara la pregunta de la
persona contra los vectores. Es INCREMENTAL: sólo se recalculan los proyectos
nuevos o cuyo texto cambió (el hash del texto decide). La primera corrida
procesa todo (unos minutos en CPU); las siguientes, segundos.

Uso:
  python scripts/generar_embeddings.py              # modelo real (sentence-transformers)
  python scripts/generar_embeddings.py --fake       # embedder de prueba (sin modelo; sólo para tests)

IMPORTANTE: el modelo del navegador (transformers.js) tiene que ser el MISMO
modelo; si se cambia MODELO acá, cambiar SEM_MODEL en generar_web.py y regenerar.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
PROYECTOS_JSON = os.path.join(DATA_DIR, "proyectos.json")
META_JSON = os.path.join(DATA_DIR, "embeddings_proyectos.json")
VEC_BIN = os.path.join(DATA_DIR, "embeddings_proyectos.bin")

AYUDA_JSON = os.path.join(DATA_DIR, "ayuda_memoria.json")
AGENDA_JSON = os.path.join(DATA_DIR, "agenda.json")
PLIEGOS_JSON = os.path.join(DATA_DIR, "pliegos_data.json")

MODELO = "intfloat/multilingual-e5-small"   # 384 dimensiones
DIM = 384
FORMATO = 1                                  # subir si cambia cómo se arma el texto


def clave(p):
    """Clave estable del proyecto. DEBE coincidir con semKey() de la web."""
    return f"{p.get('origen','')}|{p.get('tipo','')}|{p.get('nro','')}|{p.get('anio','')}"


def texto_de(p):
    """Texto que representa al proyecto (lo que se compara por significado)."""
    partes = []
    if p.get("tipo_label"):
        partes.append(p["tipo_label"])
    extracto = re.sub(r"\s+", " ", (p.get("extracto") or "")).strip()
    if extracto:
        partes.append(extracto)
    autores = [a for a in (p.get("autores") or []) if a][:4]
    if autores:
        partes.append("Autores: " + ", ".join(autores))
    coms = [c for c in (p.get("comisiones") or []) if c][:3]
    if coms:
        partes.append("Comisiones: " + ", ".join(coms))
    return ". ".join(partes)


def _sin_tildes(s):
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _fnv(s):
    h = 2166136261
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def embedder_falso(textos):
    """SÓLO PARA PRUEBAS: bolsa de palabras hasheada a 384 dimensiones. No es
    semántico; sirve para verificar el circuito completo sin descargar el modelo.
    La web tiene la misma función (window.__semEmbed en los tests)."""
    out = np.zeros((len(textos), DIM), dtype=np.float32)
    for i, t in enumerate(textos):
        for tok in re.findall(r"[a-z0-9]{3,}", _sin_tildes(t.lower())):
            h = _fnv(tok)
            out[i, h % DIM] += 1.0 if (h >> 16) & 1 else -1.0
        n = np.linalg.norm(out[i])
        if n:
            out[i] /= n
    return out


def embedder_real():
    from sentence_transformers import SentenceTransformer
    modelo = SentenceTransformer(MODELO, device="cpu")

    def f(textos):
        return modelo.encode(["passage: " + t for t in textos], batch_size=32,
                             normalize_embeddings=True, show_progress_bar=True).astype(np.float32)
    return f


def cuantizar(v):
    return np.clip(np.round(v * 127), -127, 127).astype(np.int8)


def _limpio(t):
    return re.sub(r"\s+", " ", t or "").strip()


def _cargar(path, defecto):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defecto


def items_proyectos():
    out = []
    for p in _cargar(PROYECTOS_JSON, []):
        t = texto_de(p)
        if t:
            out.append((clave(p), t))
    return out


def items_otras():
    """Ayuda Memoria, Agenda (temario de cada reunión) y Pliegos judiciales.
    Las claves DEBEN coincidir con las que arma la web (semClaveOtras)."""
    out = []
    for d in _cargar(AYUDA_JSON, []):
        t = _limpio(". ".join(x for x in [d.get("categoria"), d.get("descripcion"),
                                           ("Autor: " + d["autor"]) if d.get("autor") else "",
                                           d.get("comisionCabecera")] if x))
        if t:
            out.append((f"ayuda|{d.get('numero','')}|{d.get('periodo','')}", t))
    for r in (_cargar(AGENDA_JSON, {}) or {}).get("reuniones", []):
        rk = f"{r.get('fecha','')}|{r.get('hora_boletin') or r.get('hora') or ''}|{','.join(r.get('comisiones') or [])}"
        coms = ", ".join(r.get("comisiones") or [])
        for i, it in enumerate(r.get("temario") or []):
            t = _limpio(it.get("extracto"))
            if t:
                out.append((f"agenda|{rk}|{i}", _limpio(f"{t}. Comisión: {coms}")))
    for p in (_cargar(PLIEGOS_JSON, {}) or {}).get("pliegos", []):
        t = _limpio(". ".join(x for x in [p.get("cargo"), p.get("candidato"), p.get("provincia"), p.get("fuero")] if x))
        if t:
            out.append((f"pliego|{p.get('expediente','')}", t))
    return out


def construir(nombre, items, emb_factory, modelo_id):
    """Genera data/embeddings_<nombre>.{json,bin}, reutilizando filas sin cambios."""
    meta_json = os.path.join(DATA_DIR, f"embeddings_{nombre}.json")
    vec_bin = os.path.join(DATA_DIR, f"embeddings_{nombre}.bin")
    claves, textos, hashes, vistos = [], [], [], set()
    for k, t in items:
        if k in vistos:
            continue
        vistos.add(k)
        claves.append(k)
        textos.append(t)
        hashes.append(hashlib.md5(t.encode("utf-8")).hexdigest()[:12])
    print(f"[{nombre}] con texto: {len(claves)}")

    previas = {}
    if os.path.exists(meta_json) and os.path.exists(vec_bin):
        try:
            meta0 = json.load(open(meta_json, encoding="utf-8"))
            if meta0.get("modelo") == modelo_id and meta0.get("dim") == DIM and meta0.get("formato") == FORMATO:
                mat0 = np.fromfile(vec_bin, dtype=np.int8).reshape(-1, DIM)
                if mat0.shape[0] == len(meta0["claves"]):
                    for i, (k, h) in enumerate(zip(meta0["claves"], meta0["hashes"])):
                        previas[k] = (h, mat0[i])
        except Exception as exc:
            print(f"Aviso: no se pudo reutilizar el índice anterior ({exc}); se recalcula todo.")

    matriz = np.zeros((len(claves), DIM), dtype=np.int8)
    faltan = []
    for i, (k, h) in enumerate(zip(claves, hashes)):
        if k in previas and previas[k][0] == h:
            matriz[i] = previas[k][1]
        else:
            faltan.append(i)
    print(f"[{nombre}] reutilizados: {len(claves) - len(faltan)} | a calcular: {len(faltan)}")

    if faltan:
        emb = emb_factory()
        for ini in range(0, len(faltan), 512):
            idx = faltan[ini:ini + 512]
            matriz[idx] = cuantizar(emb([textos[i] for i in idx]))
            print(f"  {min(ini + 512, len(faltan))}/{len(faltan)}")

    os.makedirs(DATA_DIR, exist_ok=True)
    matriz.tofile(vec_bin + ".tmp")
    os.replace(vec_bin + ".tmp", vec_bin)
    meta = {"modelo": modelo_id, "dim": DIM, "formato": FORMATO, "n": len(claves),
            "claves": claves, "hashes": hashes}
    with open(meta_json + ".tmp", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(meta_json + ".tmp", meta_json)
    print(f"[{nombre}] escrito {vec_bin} ({os.path.getsize(vec_bin)/1e6:.1f} MB)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake", action="store_true", help="embedder de prueba (sin modelo)")
    args = ap.parse_args()
    modelo_id = "fake" if args.fake else MODELO
    cache = {}

    def factory():
        # el modelo se carga una sola vez aunque haya varios índices
        if "e" not in cache:
            cache["e"] = embedder_falso if args.fake else embedder_real()
        return cache["e"]

    construir("proyectos", items_proyectos(), factory, modelo_id)
    construir("otras", items_otras(), factory, modelo_id)


if __name__ == "__main__":
    main()
