"""Versículos bíblicos de sana doctrina para el cierre de mes de /resumen --
mismo espíritu que Larry la Rana y el Búho (los otros asistentes del
usuario, activamente cristianos), pero en Coco es un detalle puntual, no una
costumbre de cada mensaje (ver _persona_coco en src/llm/prompt_builder.py).

Deliberadamente NO son versículos de "teología de la prosperidad" (Dios no
promete riqueza a cambio de fe) -- son de gratitud, provisión, mayordomía y
consuelo/perseverancia, sana doctrina bíblica sobre el dinero como
herramienta y no como fin.
"""
import random

# Para meses con buen balance (ingresos > gastos, o un ahorro/meta cumplida).
VERSICULOS_BUEN_MES = [
    '"Y mi Dios suplirá todo lo que os falta conforme a sus riquezas en gloria en Cristo Jesús." '
    '(Filipenses 4:19)',
    '"Dad, y se os dará [...] porque con la medida con que medís, os volverán a medir." (Lucas 6:38)',
    '"El que es fiel en lo muy poco, también en lo más es fiel." (Lucas 16:10)',
    '"Cada uno dé como propuso en su corazón: no con tristeza, ni por necesidad, porque Dios ama '
    'al dador alegre." (2 Corintios 9:7)',
    '"Honra a Jehová con tus bienes [...] y serán llenos tus graneros con abundancia." (Proverbios 3:9-10)',
]

# Para meses apretados o con más gasto que ingreso -- de aliento, no de culpa.
VERSICULOS_MAL_MES = [
    '"No os afanéis por vuestra vida, qué habéis de comer [...] vuestro Padre celestial sabe que '
    'tenéis necesidad de todas estas cosas." (Mateo 6:25-32)',
    '"No te desampararé, ni te dejaré." (Hebreos 13:5)',
    '"He estado joven, y he envejecido, y no he visto justo desamparado." (Salmos 37:25)',
    '"Todo lo puedo en Cristo que me fortalece." (Filipenses 4:13)',
    '"Echando toda vuestra ansiedad sobre él, porque él tiene cuidado de vosotros." (1 Pedro 5:7)',
]


def pick_verse(buen_mes: bool) -> str:
    """Elige al azar un versículo apropiado para el balance del mes."""
    fuente = VERSICULOS_BUEN_MES if buen_mes else VERSICULOS_MAL_MES
    return random.choice(fuente)
