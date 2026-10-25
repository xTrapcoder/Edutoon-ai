"""Write the sample inputs used to try every generation mode by hand (``var/qa_inputs/``).

Nothing here is a fixture a test depends on, and none of it is ever offered to the product as a
pre-made result: they are ordinary files you upload or paste in the browser, like any of your own.

- ``plate_tectonics.pdf`` - a four-page, text-based earth-science chapter (PDF, PDF + keywords and
  PDF + instructions runs).
- ``water_cycle_scanned.pdf`` - three pages that are *pictures* of text with no text layer, so only
  OCR can read them (scanned-PDF run).
- ``industrial_revolution.txt`` - about 560 words of history notes (paste-text run).

Three more subjects, so a run is never only the ones a fix was developed against:

- ``human_heart.pdf`` - a three-page, text-based biology chapter (PDF, PDF + keywords).
- ``solar_system_scanned.pdf`` - three pictures-of-text pages (scanned-PDF run).
- ``python_basics.txt`` - about 400 words of programming notes (paste-text run).

    uv run python scripts/make_qa_inputs.py
"""

from __future__ import annotations

import io
import textwrap
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).resolve().parents[1] / "var" / "qa_inputs"

PLATE_TECTONICS = [
    (
        "Chapter 4: Plate Tectonics",
        [
            "The Earth is built in layers. The thin outer shell is the crust, which is about 5 to "
            "10 kilometres thick under the oceans and 30 to 50 kilometres thick under the "
            "continents. Beneath the crust lies the mantle, a thick layer of hot rock that makes "
            "up most of the Earth's volume. At the centre is the core, made mostly of iron.",
            "The crust and the rigid uppermost part of the mantle together form the lithosphere. "
            "The lithosphere is broken into large pieces called tectonic plates. There are seven "
            "major plates and several smaller ones. The plates float on the asthenosphere, a "
            "layer of the upper mantle that is hot enough to flow very slowly.",
            "Tectonic plates move only a few centimetres each year, about as fast as a fingernail "
            "grows. Over millions of years this slow movement rearranges whole oceans and "
            "continents.",
        ],
    ),
    (
        "Evidence for Moving Continents",
        [
            "In 1912 the German scientist Alfred Wegener proposed the idea of continental drift. "
            "He noticed that the coastlines of South America and Africa fit together like pieces "
            "of a puzzle. He suggested that all the continents were once joined in a single "
            "supercontinent called Pangaea, which began to break apart about 200 million years "
            "ago.",
            "Wegener also pointed to fossils. Fossils of the same freshwater reptile, Mesosaurus, "
            "are found in both South America and Africa, which are now separated by the Atlantic "
            "Ocean. Matching rock formations and mountain ranges also line up across oceans.",
            "Most scientists rejected the idea at first because Wegener could not explain what "
            "force moved the continents. The explanation came in the 1960s, when studies of the "
            "ocean floor showed that new crust is being made at mid-ocean ridges. This process is "
            "called seafloor spreading. Magnetic stripes in the rock on either side of a ridge "
            "form a mirror pattern, recording how the ocean floor has spread outward over time.",
        ],
    ),
    (
        "Plate Boundaries",
        [
            "Plates meet at boundaries, and most earthquakes and volcanoes occur there. There are "
            "three main types of boundary. At a divergent boundary two plates move apart. Magma "
            "rises to fill the gap and forms new crust. The Mid-Atlantic Ridge is a divergent "
            "boundary, and Iceland sits on top of it.",
            "At a convergent boundary two plates move towards each other. When an oceanic plate "
            "meets a continental plate, the denser oceanic plate sinks beneath the other in a "
            "process called subduction. The sinking plate melts as it goes deeper, and the "
            "magma that forms can rise to build a chain of volcanoes. The Andes in South America "
            "formed this way. When two continental plates collide, neither sinks easily, so the "
            "crust crumples upwards into mountains. The Himalayas are still rising because India "
            "is pushing into Asia.",
            "At a transform boundary two plates slide past each other. The plates can stick, and "
            "when the built-up stress is suddenly released the ground shakes in an earthquake. "
            "The San Andreas Fault in California is a transform boundary.",
        ],
    ),
    (
        "Earthquakes, Volcanoes and Hotspots",
        [
            "An earthquake is the shaking caused by a sudden release of energy in the crust. The "
            "point underground where the rock first breaks is the focus, and the point on the "
            "surface directly above it is the epicentre. Seismic waves spread out from the focus "
            "and are recorded by instruments called seismographs.",
            "Most volcanoes form along plate boundaries, especially at subduction zones. The "
            "Ring of Fire is a belt around the Pacific Ocean where many subduction zones are "
            "found, and it has about three quarters of the world's active volcanoes.",
            "Some volcanoes are not on plate boundaries. A hotspot is a place where a column of "
            "unusually hot rock rises from deep in the mantle. As a plate moves slowly over a "
            "hotspot, a chain of volcanic islands forms. The Hawaiian Islands were made this way, "
            "and the oldest islands are the ones that have moved furthest from the hotspot.",
        ],
    ),
]

WATER_CYCLE = [
    (
        "The Water Cycle",
        "The water cycle describes how water moves between the oceans, the atmosphere and the "
        "land. Heat from the sun causes evaporation, which turns liquid water into water vapour. "
        "Plants also release water vapour through their leaves, a process called transpiration. "
        "As the vapour rises it cools and condenses into tiny droplets that gather to form clouds.",
    ),
    (
        "Precipitation and Collection",
        "When cloud droplets grow heavy they fall as precipitation, such as rain, snow or hail. "
        "Precipitation that lands on the ground may soak into the soil, which is called "
        "infiltration, or flow over the surface as runoff. Runoff gathers in streams and rivers "
        "and eventually returns to the ocean. Water that soaks deep into the ground becomes "
        "groundwater, which moves slowly through porous rock.",
    ),
    (
        "Water Storage",
        "Most of the water on Earth is stored in the oceans, which hold about 97 percent of it. "
        "Most of the fresh water is locked in glaciers and ice sheets. Only a very small share is "
        "found in rivers and lakes. The sun powers the whole cycle, so water is constantly moved "
        "from one store to another and is never used up.",
    ),
]

INDUSTRIAL_REVOLUTION = """\
The Industrial Revolution: Study Notes

The Industrial Revolution was the period when production moved from hand tools and small
workshops to machines and factories. It began in Britain in the second half of the eighteenth
century and spread to Europe, North America and later the rest of the world. It changed how
people worked, where they lived and how goods were made.

Why it began in Britain

Several conditions came together in Britain. The country had large deposits of coal and iron ore
close to each other and close to ports. It had a stable government, a growing banking system and
a large overseas empire that supplied raw materials such as cotton and bought finished goods.
Agricultural improvements meant that fewer farm workers were needed to feed the population, so
many people were free to move to towns to find work.

The textile industry

Textiles were the first industry to be transformed. Spinning and weaving had been done by hand at
home. In 1764 James Hargreaves invented the spinning jenny, which let one worker spin several
threads at once. In 1769 Richard Arkwright patented the water frame, a spinning machine powered
by running water, and he built the first large factories to house it. In 1785 Edmund Cartwright
patented the power loom, which made weaving much faster. Cloth that once took weeks to make could
now be produced in days.

Steam power

Early factories sat beside rivers because they needed water wheels for power. The steam engine
removed that limit. Thomas Newcomen built an engine in 1712 to pump water out of mines, but it
used a great deal of coal. James Watt improved the design in the 1760s and 1770s by adding a
separate condenser, which made the engine far more efficient. With steam engines, factories could
be built anywhere, and coal became the fuel of the new age.

Iron and transport

Better ways of making iron were also important. Abraham Darby used coke instead of charcoal to
smelt iron in 1709, which made iron cheaper and stronger. Iron was used for machines, bridges and
rails. Transport improved as well. Canals carried heavy loads of coal and goods cheaply. In 1825
the Stockton and Darlington Railway opened as the first public railway to use steam locomotives,
and railways spread quickly across the country. They moved people and goods faster than ever and
linked factories to ports and markets.

Life in the new towns

Factory towns grew very fast. Manchester, Birmingham and Leeds filled with workers, and the
population of cities rose sharply. Housing was often crowded and badly built, with little clean
water or sanitation, and diseases such as cholera spread easily. Factory work was long and
disciplined. Workers, including many women and children, often worked twelve hours or more a day
in dangerous conditions for low pay.

Reform and its effects

Over time, public concern led to reforms. The Factory Act of 1833 limited the working hours of
children and required some schooling. Later laws improved safety and shortened the working day.
Workers formed trade unions to bargain for better pay and conditions. Although the early years
were hard for many people, the Industrial Revolution eventually raised living standards, created
new kinds of jobs and made goods cheaper. It also began the large-scale burning of coal, which
started the rise in carbon dioxide in the atmosphere that continues today.
"""


HUMAN_HEART = [
    (
        "The Human Heart",
        [
            "The human heart is a muscular organ about the size of a fist. It sits in the chest "
            "between the lungs, slightly to the left. The heart pumps blood around the body. The "
            "blood delivers oxygen and nutrients to the cells and carries away carbon dioxide and "
            "other waste. An adult heart beats about 60 to 100 times a minute at rest.",
            "The heart has four chambers. The two upper chambers are called atria and the two "
            "lower chambers are called ventricles. The right side of the heart receives blood that "
            "is low in oxygen and pumps it to the lungs. The left side receives blood rich in "
            "oxygen from the lungs and pumps it to the rest of the body. The wall of the left "
            "ventricle is thicker than the wall of the right ventricle because it must push blood "
            "around the whole body.",
        ],
    ),
    (
        "Valves and the Heartbeat",
        [
            "Four valves keep blood flowing in one direction. The tricuspid valve and the mitral "
            "valve lie between the atria and the ventricles. The pulmonary valve and the aortic "
            "valve lie where blood leaves the ventricles. The familiar lub-dub sound of a "
            "heartbeat is made by the valves closing.",
            "Each heartbeat is started by a small group of cells in the right atrium called the "
            "sinoatrial node, which acts as the natural pacemaker. It sends out an electrical "
            "signal that makes the atria contract. The signal then reaches the atrioventricular "
            "node, which passes it on to the ventricles so that they contract a moment later. "
            "This delay lets the ventricles fill with blood before they pump.",
        ],
    ),
    (
        "Blood Vessels and Circulation",
        [
            "Arteries carry blood away from the heart and veins carry blood back to it. "
            "Capillaries are the tiny vessels where oxygen and nutrients pass into the tissues. "
            "The aorta is the largest artery in the body. Blood makes two circuits. In pulmonary "
            "circulation it travels from the heart to the lungs and back. In systemic circulation "
            "it travels from the heart to the rest of the body and back.",
            "Blood pressure is written as two numbers, such as 120 over 80. The first is the "
            "systolic pressure, measured when the heart beats. The second is the diastolic "
            "pressure, measured between beats. The coronary arteries supply the heart muscle "
            "itself, and a blocked coronary artery can cause a heart attack. Regular exercise "
            "strengthens the heart so that it pumps more blood with each beat.",
        ],
    ),
]

SOLAR_SYSTEM = [
    (
        "The Solar System",
        "The solar system is made up of the Sun and everything that orbits it. The Sun is a star "
        "that holds about 99 percent of the mass of the solar system. Eight planets orbit the Sun. "
        "The four inner planets, Mercury, Venus, Earth and Mars, are small and rocky. The four "
        "outer planets, Jupiter, Saturn, Uranus and Neptune, are much larger. Jupiter and Saturn "
        "are gas giants, while Uranus and Neptune are ice giants.",
    ),
    (
        "Moons and Small Bodies",
        "Most planets have moons. Earth has one moon, which takes about 27 days to orbit our "
        "planet. Mars has two small moons, Phobos and Deimos. Between Mars and Jupiter lies the "
        "asteroid belt, a ring of rocky objects. Beyond Neptune is the Kuiper Belt, which holds "
        "icy bodies such as Pluto, now classed as a dwarf planet. Comets are balls of ice and dust "
        "that grow glowing tails when they come near the Sun.",
    ),
    (
        "Distances and Time",
        "Earth is about 150 million kilometres from the Sun, a distance called one astronomical "
        "unit. Light from the Sun takes about eight minutes to reach Earth. A year on a planet is "
        "the time it takes to orbit the Sun. Earth takes 365 days, Mercury takes only 88 days and "
        "Neptune takes about 165 Earth years. Venus spins so slowly that one day there lasts "
        "longer than a Venusian year.",
    ),
]

PYTHON_BASICS = """\
Python Basics: Study Notes

Python is a programming language that is popular with beginners because its code is easy to
read. It was created by Guido van Rossum and first released in 1991. A Python program is a text
file that an interpreter runs one line at a time.

Variables and types

A variable is a name that refers to a value. You create one by assigning to it, for example
age = 15. Python works out the type of the value for itself. The basic types are integers such as
42, floating point numbers such as 3.14, strings such as "hello" and booleans, which are either
True or False. The type() function tells you which type a value has.

Lists and dictionaries

A list holds an ordered collection of items inside square brackets, for example [3, 1, 2]. Items
are counted from zero, so the first item is at index 0. Lists can grow with the append() method.
A dictionary stores pairs of keys and values inside curly brackets. You look up a value by its
key, so a dictionary of ages could be asked for the age of one person by name.

Decisions and loops

An if statement runs a block of code only when a condition is true, and else covers the other
case. A for loop repeats a block once for each item in a collection. A while loop repeats for as
long as a condition stays true. Python uses indentation, not braces, to show which lines belong
to a block, so a missing indent is an error.

Functions

A function is a named block of code that you can run again whenever you need it. You define one
with the def keyword, list its parameters in brackets and use return to give a value back.
Writing functions stops you repeating code and makes a program easier to test. Python also comes
with built-in functions such as len(), which counts the items in a collection, and print(), which
shows a value on the screen.

Errors

When Python cannot run a line it raises an error and prints a traceback that shows where the
problem happened. A syntax error means the code was written wrongly. An exception such as
ZeroDivisionError happens while the program is running. A try and except block lets a program
catch an exception and carry on instead of stopping.
"""


def _text_pdf(chapters: list[tuple[str, list[str]]]) -> bytes:
    document = FPDF()
    document.set_auto_page_break(auto=True, margin=15)
    for heading, paragraphs in chapters:
        document.add_page()
        document.set_font("Helvetica", style="B", size=18)
        document.multi_cell(0, 10, heading)
        document.ln(3)
        document.set_font("Helvetica", size=12)
        for paragraph in paragraphs:
            document.multi_cell(0, 6.5, paragraph)
            document.ln(3)
    return bytes(document.output())


def _scanned_pdf(pages: list[tuple[str, str]]) -> bytes:
    heading_font = ImageFont.load_default(size=52)
    body_font = ImageFont.load_default(size=36)
    images = []
    for heading, body in pages:
        image = Image.new("L", (1240, 1754), 245)
        draw = ImageDraw.Draw(image)
        draw.text((100, 120), heading, font=heading_font, fill=20)
        y = 250
        for line in textwrap.wrap(body, 56):
            draw.text((100, y), line, font=body_font, fill=30)
            y += 56
        images.append(image.filter(ImageFilter.GaussianBlur(0.8)).convert("RGB"))
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:], resolution=150)
    return buffer.getvalue()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plate_tectonics.pdf").write_bytes(_text_pdf(PLATE_TECTONICS))
    (OUT / "water_cycle_scanned.pdf").write_bytes(_scanned_pdf(WATER_CYCLE))
    (OUT / "industrial_revolution.txt").write_text(INDUSTRIAL_REVOLUTION, encoding="utf-8")
    (OUT / "human_heart.pdf").write_bytes(_text_pdf(HUMAN_HEART))
    (OUT / "solar_system_scanned.pdf").write_bytes(_scanned_pdf(SOLAR_SYSTEM))
    (OUT / "python_basics.txt").write_text(PYTHON_BASICS, encoding="utf-8")
    for path in sorted(OUT.iterdir()):
        print(f"{path}  ({path.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
