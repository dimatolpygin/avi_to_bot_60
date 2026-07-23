# -*- coding: utf-8 -*-
"""Замер качества поиска одной командой (этап 7).

    python -m bot.search.baseline            # прогон и сводка
    python -m bot.search.baseline --save     # плюс отчёт в docs/BASELINE.md
    python -m bot.search.baseline --iz-bd    # каталог из products, а не из файла
    python -m bot.search.baseline --podrobno # показать выдачу по каждому провалу

Это не разовая проверка этапа, а инструмент до конца проекта. Правило, из-за
которого он вообще написан: **после каждой правки словаря, весов или логики
поиска — прогнать baseline. Метрика упала → правку откатить или починить.**
Так велась работа в telewin, и на 12 000 позиций это единственное, что удержало
качество; здесь позиций 41, но цена ошибки та же — старый бот клиента провалился
ровно на подмене сорта и размера.

Три набора, три разных вопроса к поиску:

* **золотой** (`data/zolotoy_nabor.json`) — правильный ответ выверен глазами
  по прайсу. Ворота этапа: top-1 ≥ 90%. Сюда же навсегда дописывается каждый
  провал из живого диалога клиента.
* **слепой** (`data/test_nabor.json`) — разговорные формулировки, на которых
  поиск не настраивался. Проверяется только семейство в топ-1: у «нужна вагонка
  в парную» правильных ответов четыре, и выбор между ними — работа менеджера.
  Порога нет, набор показывает покрытие и ловит обвал.
* **абстейн** (`data/abstain_nabor.json`) — чужой домен, правильный ответ пустой.
  Ворота этапа: ≥ 90%, и не «просто пусто», а именно через гейт чужого домена:
  «мы этим не занимаемся» и «такого нет в наличии» — разные ответы клиенту.

Что считается провалом top-1, кроме неверного товара: **неверная длина**.
«Показал вагонку липа А, но метровую вместо трёхметровой» — это 171 ₽ вместо
513, то есть враньё по цене, а не мелкая неточность.

**Порогов мало — нужен предыдущий замер.** Ворота этапа (90%) переживают потерю
четырёх запросов из сорока одного: убрать из словаря маппинг «сорт Б» → «сорт В»
и метрика упадёт со 100% до 95%, то есть бот начнёт отвечать «нет такого» на
живой вопрос клиента, а ворота этого не заметят. Поэтому `--save` кладёт цифры
в `docs/BASELINE.md` машиночитаемой строкой, а следующий прогон показывает
дельту и считает ЛЮБОЕ падение провалом. Правило «метрика упала → правку
откатить или починить» держится именно на этом сравнении, а не на пороге.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..logger import logger
from .katalog import Katalog, iz_fayla_praysa
from .search import Nahodka, Poisk
from .slovari import KATALOG_DANNYH, Slovari

# Ворота этапа 7. Меняются только вместе с решением человека, не «чтобы прошло».
PORG_ZOLOTOY_TOP1 = 0.90
PORG_ABSTEYNA = 0.90

KOREN = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OTCHET = os.path.join(KOREN, "docs", "BASELINE.md")

ZOLOTOY = "zolotoy_nabor.json"
SLEPOY = "test_nabor.json"
ABSTEYN = "abstain_nabor.json"


# ── Результаты ───────────────────────────────────────────────────────────────


@dataclass
class Zapis:
    """Итог одного запроса: что спросили, что получили, что не сошлось."""

    zapros: str
    kanal: str
    vydacha: list[Nahodka]
    top1_ok: bool = False
    top5_ok: bool = False
    kanal_ok: bool = True
    zapret_narushen: bool = False
    prichiny: list[str] = field(default_factory=list)

    @property
    def proval(self) -> bool:
        return not self.top1_ok or not self.top5_ok or self.zapret_narushen or not self.kanal_ok

    def kratko_vydacha(self, skolko: int = 3) -> str:
        if not self.vydacha:
            return "(пусто)"
        return "; ".join(f"{n.gruppa.imya} [{n.ball:.1f}]" for n in self.vydacha[:skolko])


@dataclass
class Svodka:
    """Метрики по набору целиком."""

    imya: str
    zapisi: list[Zapis]

    @property
    def vsego(self) -> int:
        return len(self.zapisi)

    def _dolya(self, skolko: int) -> float:
        return skolko / self.vsego if self.vsego else 0.0

    @property
    def top1(self) -> float:
        return self._dolya(sum(1 for z in self.zapisi if z.top1_ok))

    @property
    def top5(self) -> float:
        return self._dolya(sum(1 for z in self.zapisi if z.top5_ok))

    @property
    def kanaly(self) -> float:
        return self._dolya(sum(1 for z in self.zapisi if z.kanal_ok))

    @property
    def narusheniy_zapreta(self) -> int:
        return sum(1 for z in self.zapisi if z.zapret_narushen)

    @property
    def provaly(self) -> list[Zapis]:
        return [z for z in self.zapisi if z.proval]


# ── Загрузка наборов ─────────────────────────────────────────────────────────


def _chitat_nabor(imya: str, katalog: str | None = None) -> list[dict]:
    put = os.path.join(katalog or KATALOG_DANNYH, imya)
    if not os.path.exists(put):
        raise FileNotFoundError(
            f"❌ Не найден набор {imya} в {katalog or KATALOG_DANNYH}. "
            f"Он лежит в репозитории (data/), проверь рабочий каталог.")
    with open(put, encoding="utf-8") as f:
        return json.load(f)["запросы"]


# ── Прогон золотого набора ───────────────────────────────────────────────────


def _imena(vydacha: list[Nahodka]) -> list[str]:
    return [n.gruppa.imya for n in vydacha]


def _proverit_zolotoy(poisk: Poisk, e: dict) -> Zapis:
    vydacha, kanal = poisk.iskat(e["запрос"])
    z = Zapis(zapros=e["запрос"], kanal=kanal, vydacha=vydacha)
    imena = _imena(vydacha)

    ozhidaemy_kanal = e.get("канал")
    if ozhidaemy_kanal and kanal != ozhidaemy_kanal:
        z.kanal_ok = False
        z.prichiny.append(f"канал «{kanal}», ждали «{ozhidaemy_kanal}»")

    # Пустая выдача как правильный ответ: «штиль» в прайсе нет, и подсунуть
    # вместо него обычную вагонку — это баг старого бота, а не забота.
    if e.get("пусто"):
        z.top1_ok = z.top5_ok = not vydacha
        if vydacha:
            z.prichiny.append(f"ждали пустую выдачу, пришло: {z.kratko_vydacha()}")
        return z

    if not vydacha:
        z.prichiny.append("выдача пустая, а товар в прайсе есть")
        return z

    pervy = vydacha[0]
    if pervy.gruppa.imya in e["топ1"]:
        z.top1_ok = True
    else:
        z.prichiny.append(f"в топ-1 «{pervy.gruppa.imya}»")

    # Длина — часть правильного ответа, а не деталь: метровая вагонка вместо
    # трёхметровой это 171 ₽ вместо 513.
    if z.top1_ok and "длина" in e:
        zhdem = Decimal(e["длина"])
        if pervy.pozitsiya.length_m != zhdem:
            z.top1_ok = False
            z.prichiny.append(f"длина {pervy.pozitsiya.length_m}, ждали {zhdem}")
    if z.top1_ok and e.get("длины_нет") and pervy.dlina_sovpala:
        z.top1_ok = False
        z.prichiny.append("ждали пометку «спрошенной длины нет», товар пришёл как совпавший")

    # hit@5: всё перечисленное обязано быть в пятёрке. Если список не задан —
    # достаточно, чтобы в пятёрку попал хоть один из допустимых для топ-1.
    v_pyaterke = set(imena[:5])
    nuzhno = e.get("в_топ5") or []
    if nuzhno:
        ne_hvataet = [n for n in nuzhno if n not in v_pyaterke]
        z.top5_ok = not ne_hvataet
        if ne_hvataet:
            z.prichiny.append("нет в топ-5: " + "; ".join(ne_hvataet))
    else:
        z.top5_ok = bool(v_pyaterke & set(e["топ1"]))
        if not z.top5_ok:
            z.prichiny.append("ни одного ожидаемого товара в топ-5")

    # Запреты. Это половина ценности набора: старый бот врал не пустой выдачей,
    # а подменой сорта и размера — показывал А вместо В и 2000х800 вместо 1900х700.
    lishnie = [n for n in e.get("не_должно", []) if n in imena]
    if lishnie:
        z.zapret_narushen = True
        z.prichiny.append("в выдаче запрещённое: " + "; ".join(lishnie))
    return z


# ── Прогон слепого набора ────────────────────────────────────────────────────


def _proverit_slepoy(poisk: Poisk, e: dict) -> Zapis:
    vydacha, kanal = poisk.iskat(e["запрос"])
    z = Zapis(zapros=e["запрос"], kanal=kanal, vydacha=vydacha)

    if e.get("пусто_ок"):
        z.top1_ok = z.top5_ok = not vydacha
        if vydacha:
            z.prichiny.append(f"такого товара у нас нет, а поиск выдал: {z.kratko_vydacha()}")
        return z

    if not vydacha:
        z.prichiny.append("пусто")
        return z

    zhdem = e["семейство"]
    poluchili = vydacha[0].gruppa.family
    z.top1_ok = poluchili == zhdem
    if not z.top1_ok:
        z.prichiny.append(f"семейство «{poluchili}», ждали «{zhdem}» ({vydacha[0].gruppa.imya})")
    # В пятёрке — хоть один товар нужного семейства.
    z.top5_ok = any(n.gruppa.family == zhdem for n in vydacha[:5])
    if not z.top5_ok:
        z.prichiny.append(f"в топ-5 ни одного товара семейства «{zhdem}»")
    return z


# ── Прогон абстейна ──────────────────────────────────────────────────────────


def _proverit_absteyn(poisk: Poisk, e: dict) -> Zapis:
    vydacha, kanal = poisk.iskat(e["запрос"])
    z = Zapis(zapros=e["запрос"], kanal=kanal, vydacha=vydacha)
    z.top1_ok = z.top5_ok = not vydacha
    if vydacha:
        z.prichiny.append(f"ждали пусто, пришло: {z.kratko_vydacha()}")
    # Пусто через «не найдено» — половина беды: клиенту скажут «нет в наличии»
    # вместо «мы этим не занимаемся», и разговор пойдёт не туда.
    elif kanal != "чужой домен":
        z.kanal_ok = False
        z.prichiny.append(f"пусто, но каналом «{kanal}», а не гейтом чужого домена")
    return z


# ── Оркестровка ──────────────────────────────────────────────────────────────


def progon(katalog: Katalog, sl: Slovari | None = None,
           katalog_dannyh: str | None = None) -> dict[str, Svodka]:
    """Прогнать все три набора. Возвращает сводки по именам наборов."""
    poisk = Poisk(katalog, sl)
    return {
        "золотой": Svodka("золотой", [
            _proverit_zolotoy(poisk, e) for e in _chitat_nabor(ZOLOTOY, katalog_dannyh)]),
        "слепой": Svodka("слепой", [
            _proverit_slepoy(poisk, e) for e in _chitat_nabor(SLEPOY, katalog_dannyh)]),
        "абстейн": Svodka("абстейн", [
            _proverit_absteyn(poisk, e) for e in _chitat_nabor(ABSTEYN, katalog_dannyh)]),
    }


def vorota_proydeny(svodki: dict[str, Svodka]) -> bool:
    """Пройдены ли ворота этапа 7: золотой top-1 и абстейн ≥ 90%."""
    return (svodki["золотой"].top1 >= PORG_ZOLOTOY_TOP1
            and svodki["абстейн"].top1 >= PORG_ABSTEYNA
            and svodki["золотой"].narusheniy_zapreta == 0)


# ── Сравнение с предыдущим замером ───────────────────────────────────────────

# Строка-снимок в конце отчёта. Читается следующим прогоном, человеком —
# не читается, поэтому спрятана в html-комментарий.
_METKA_SNIMKA = "<!-- снимок: "

# Меньше этого расхождения считаем шумом округления, а не регрессом.
EPSILON = 1e-9


def snimok(svodki: dict[str, Svodka]) -> dict[str, float]:
    return {
        "золотой_top1": svodki["золотой"].top1,
        "золотой_hit5": svodki["золотой"].top5,
        "слепой_top1": svodki["слепой"].top1,
        "абстейн_top1": svodki["абстейн"].top1,
        "нарушений": float(svodki["золотой"].narusheniy_zapreta),
    }


def predydushchiy_snimok(put: str = OTCHET) -> dict[str, float] | None:
    """Цифры последнего сохранённого замера или None, если отчёта ещё нет."""
    if not os.path.exists(put):
        return None
    with open(put, encoding="utf-8") as f:
        for stroka in f:
            if stroka.startswith(_METKA_SNIMKA):
                syroy = stroka[len(_METKA_SNIMKA):].rsplit("-->", 1)[0].strip()
                try:
                    return json.loads(syroy)["цифры"]
                except (ValueError, KeyError):
                    return None
    return None


def regressii(svodki: dict[str, Svodka],
              bylo: dict[str, float] | None) -> list[tuple[str, float, float]]:
    """Метрики, просевшие относительно прошлого замера: (имя, было, стало).

    Нарушения запретов растут, а не падают, поэтому сравниваются наоборот.
    """
    if not bylo:
        return []
    stalo = snimok(svodki)
    upavshie = []
    for imya, novoe in stalo.items():
        staroe = bylo.get(imya)
        if staroe is None:
            continue
        huzhe = novoe > staroe + EPSILON if imya == "нарушений" else novoe < staroe - EPSILON
        if huzhe:
            upavshie.append((imya, staroe, novoe))
    return upavshie


# ── Печать ───────────────────────────────────────────────────────────────────


def _protsent(v: float) -> str:
    return f"{v * 100:5.1f}%"


def _pechat(svodki: dict[str, Svodka], podrobno: bool,
            bylo: dict[str, float] | None = None) -> None:
    z, s, a = svodki["золотой"], svodki["слепой"], svodki["абстейн"]
    upavshie = regressii(svodki, bylo)

    print("\n" + "═" * 74)
    print("ЗАМЕР КАЧЕСТВА ПОИСКА")
    print("═" * 74)
    print(f"{'набор':<10} {'запросов':>9} {'top-1':>8} {'hit@5':>8} {'канал':>8}  порог")
    print("─" * 74)
    print(f"{'золотой':<10} {z.vsego:>9} {_protsent(z.top1):>8} {_protsent(z.top5):>8} "
          f"{_protsent(z.kanaly):>8}  top-1 ≥ {_protsent(PORG_ZOLOTOY_TOP1).strip()}")
    print(f"{'слепой':<10} {s.vsego:>9} {_protsent(s.top1):>8} {_protsent(s.top5):>8} "
          f"{'—':>8}  порога нет (покрытие)")
    print(f"{'абстейн':<10} {a.vsego:>9} {_protsent(a.top1):>8} {'—':>8} "
          f"{_protsent(a.kanaly):>8}  пусто ≥ {_protsent(PORG_ABSTEYNA).strip()}")
    print("─" * 74)
    print(f"Нарушений запретов в золотом наборе: {z.narusheniy_zapreta} "
          f"(допустимо 0 — это подмена сорта или размера)")

    for svodka in (z, s, a):
        if not svodka.provaly:
            continue
        print(f"\n▼ Расхождения, набор «{svodka.imya}» ({len(svodka.provaly)} из {svodka.vsego}):")
        for p in svodka.provaly:
            print(f"  ✗ {p.zapros!r} — {'; '.join(p.prichiny) or 'см. выдачу'}")
            if podrobno:
                print(f"      канал: {p.kanal}")
                for i, n in enumerate(p.vydacha[:5], 1):
                    print(f"      {i}. [{n.ball:5.1f}] {n.kratko()}")

    print()
    if bylo is None:
        print("ℹ  Предыдущего замера нет (docs/BASELINE.md не сохранён) — "
              "сравнивать не с чем, запусти с --save.")
    elif upavshie:
        print("▼ ПРОСЕЛО относительно прошлого замера:")
        for imya, staroe, novoe in upavshie:
            if imya == "нарушений":
                print(f"  ✗ нарушений запретов: было {staroe:.0f}, стало {novoe:.0f}")
            else:
                print(f"  ✗ {imya}: было {_protsent(staroe).strip()}, "
                      f"стало {_protsent(novoe).strip()}")
        print("  Правку словаря или весов откатить либо починить — "
              "«посмотрим потом» здесь не работает.")
    else:
        print("▲ Относительно прошлого замера ничего не просело.")

    print()
    if vorota_proydeny(svodki) and not upavshie:
        print("✅ Ворота этапа 7 пройдены, регресса нет.")
    elif not vorota_proydeny(svodki):
        print("❌ Ворота этапа 7 НЕ пройдены — правку поиска откатить или починить.")
    else:
        print("❌ Ворота пройдены, но метрика просела — это всё равно регресс.")
    print()


# ── Отчёт ────────────────────────────────────────────────────────────────────


def _otchet_md(svodki: dict[str, Svodka], istochnik: str) -> str:
    z, s, a = svodki["золотой"], svodki["слепой"], svodki["абстейн"]
    kogda = datetime.now().strftime("%d.%m.%Y %H:%M")
    stroki = [
        "# BASELINE — замер качества поиска",
        "",
        "> Файл генерируется командой `python -m bot.search.baseline --save`.",
        "> Руками не правится: цифры здесь — снимок последнего прогона.",
        "",
        f"**Замер**: {kogda}  ",
        f"**Каталог**: {istochnik}  ",
        f"**Ворота этапа 7**: золотой top-1 ≥ {_protsent(PORG_ZOLOTOY_TOP1).strip()}, "
        f"абстейн ≥ {_protsent(PORG_ABSTEYNA).strip()}, нарушений запретов 0 — "
        f"{'ПРОЙДЕНЫ' if vorota_proydeny(svodki) else 'НЕ ПРОЙДЕНЫ'}",
        "",
        "| набор | запросов | top-1 | hit@5 | канал | что меряет |",
        "|---|---|---|---|---|---|",
        f"| золотой | {z.vsego} | **{_protsent(z.top1).strip()}** | {_protsent(z.top5).strip()} "
        f"| {_protsent(z.kanaly).strip()} | правильная позиция, выверенная по прайсу глазами |",
        f"| слепой | {s.vsego} | {_protsent(s.top1).strip()} | {_protsent(s.top5).strip()} | — "
        f"| семейство в топ-1 на формулировках, где поиск не настраивался |",
        f"| абстейн | {a.vsego} | **{_protsent(a.top1).strip()}** | — | {_protsent(a.kanaly).strip()} "
        f"| пустая выдача на чужой домен; «канал» — доля через гейт, а не через «не найдено» |",
        "",
        f"Нарушений запретов в золотом наборе: **{z.narusheniy_zapreta}** "
        f"(подмена сорта или размера; допустимо 0).",
        "",
        "## Как читать эти цифры",
        "",
        "Высокий процент здесь — **не доказательство, что поиск хорош вообще**. "
        "Наборы писал тот же человек, что и словари, значит формулировки в них "
        "смещены в сторону того, что словари уже знают. Настоящую проверку даст "
        "первый живой диалог с клиентом.",
        "",
        "Цифры работают на другое: **любое падение относительно прошлого замера "
        "означает, что правка что-то сломала**. Именно для этого в конце файла "
        "лежит снимок — следующий прогон сравнивает себя с ним и падает с кодом 1, "
        "даже если ворота формально пройдены.",
        "",
        "Провал из живого диалога дописывается в `data/zolotoy_nabor.json` "
        "и остаётся там навсегда.",
        "",
    ]
    for svodka in (z, s, a):
        if not svodka.provaly:
            stroki += [f"## Набор «{svodka.imya}»: расхождений нет", ""]
            continue
        stroki += [f"## Набор «{svodka.imya}»: расхождения "
                   f"({len(svodka.provaly)} из {svodka.vsego})", "",
                   "| запрос | что не так | выдача (топ-3) |", "|---|---|---|"]
        for p in svodka.provaly:
            prichiny = "; ".join(p.prichiny) or "—"
            stroki.append(f"| `{p.zapros}` | {prichiny} | {p.kratko_vydacha()} |")
        stroki.append("")
    # Машиночитаемый хвост: его читает следующий прогон, чтобы показать дельту.
    # Порога мало — просадка со 100% до 95% ворота проходит, а клиента теряет.
    stroki += [_METKA_SNIMKA + json.dumps(
        {"когда": kogda, "цифры": snimok(svodki)}, ensure_ascii=False) + " -->", ""]
    return "\n".join(stroki)


def sohranit(svodki: dict[str, Svodka], istochnik: str, put: str = OTCHET) -> str:
    os.makedirs(os.path.dirname(put), exist_ok=True)
    with open(put, "w", encoding="utf-8", newline="\n") as f:
        f.write(_otchet_md(svodki, istochnik))
    return put


# ── CLI ──────────────────────────────────────────────────────────────────────


async def _katalog_iz_bd(kod_akkaunta: str) -> Katalog:
    from ..config import load_config
    from ..db import podklyuchit, sozdat_fabriku_sessiy, zakryt
    from .katalog import zagruzit_iz_bd

    cfg = load_config()
    engine = await podklyuchit(cfg)
    try:
        Sessiya = sozdat_fabriku_sessiy(engine)
        async with Sessiya() as sess:
            return await zagruzit_iz_bd(sess, kod_akkaunta)
    finally:
        await zakryt(engine)


def main(argv: list[str]) -> int:
    import asyncio

    iz_bd = "--iz-bd" in argv
    katalog = asyncio.run(_katalog_iz_bd("saunamart")) if iz_bd else iz_fayla_praysa()

    bylo = predydushchiy_snimok()
    svodki = progon(katalog)
    _pechat(svodki, podrobno="--podrobno" in argv, bylo=bylo)

    if "--save" in argv:
        put = sohranit(svodki, katalog.istochnik)
        print(f"📝 Отчёт записан: {os.path.relpath(put, KOREN)}\n")

    # Код возврата — часть инструмента: и провал ворот, и просадка относительно
    # прошлого замера видны человеку и CI, а не тонут в сводке.
    return 0 if (vorota_proydeny(svodki) and not regressii(svodki, bylo)) else 1


if __name__ == "__main__":
    logger.info("Замер качества поиска (этап 7)")
    raise SystemExit(main(sys.argv))
