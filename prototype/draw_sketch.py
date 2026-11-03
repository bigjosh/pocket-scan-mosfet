"""Generate revision G: fitted 1k 5% drain filter and relay-switched 18 V rail.

Reference data:
https://www.ti.com/lit/ds/symlink/lm358.pdf (legacy LM358)
https://www.ti.com/lit/ds/symlink/ina226.pdf
https://datasheets.raspberrypi.com/picow/pico-2-w-datasheet.pdf

Both channels use 100 kHz PWM and 68k/10k feedback. Drain R13 adds the
3.3 V reference through 6.8k at IN1+; fitted drain filter R1 is 1k 5%.
Gate remains ground referenced with its nominal 1k filter, not remeasured.
Two 9 V batteries are series connected, negative at GND and positive through
a normally open relay-module contact to U1 pin 8. Module VCC and ACTIVATE
both use 3.3 V; its GND uses common ground. Internal suppression is unverified.
The relay module is a power switch, not a software-controlled output interlock.
Source reference is manual in firmware; ADC3 does not read this 3.3 V rail.
C5 is fitted; C6 installation is unconfirmed. Recorded calibration and hardware
limitations are in firmware/README.md. The fixture numbering is not DUT pin order:
onsemi 2N7000G, flat marked face toward you, leads down, is 1=S, 2=G, 3=D.
"""
from pathlib import Path
from html import escape

OUT = Path(__file__).parent
INK = '#172b3a'
WIRE = '#236b69'
MUTED = '#526674'
ACCENT = '#a95018'


class Sheet:
    def __init__(self, name, width, height, title, subtitle):
        self.name, self.w, self.h = name, width, height
        self.items = [
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
            f'<title>{escape(title)}</title>',
            '<style>text{font-family:Arial,Helvetica,sans-serif}.wire{fill:none;stroke:#236b69;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}</style>',
            f'<rect width="{width}" height="{height}" fill="#fff"/>',
        ]
        self.text(40, 49, title, 30, weight='bold')
        self.text(40, 81, subtitle, 18, MUTED)

    def raw(self, value):
        self.items.append(value)

    def text(self, x, y, value, size=18, color=INK, anchor='start', weight='normal'):
        self.raw(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" text-anchor="{anchor}" font-weight="{weight}">{escape(str(value))}</text>')

    def lines(self, x, y, values, size=18, color=INK, gap=27):
        for index, value in enumerate(values):
            self.text(x, y + index * gap, value, size, color)

    def box(self, x, y, width, height, title=None, fill='#f5f8f8'):
        self.raw(f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="7" fill="{fill}" stroke="#cbd7db" stroke-width="1.3"/>')
        if title:
            self.text(x + 20, y + 32, title, 21, weight='bold')

    def wire(self, *points):
        self.raw('<polyline class="wire" points="' + ' '.join(f'{x},{y}' for x, y in points) + '"/>')

    def dot(self, x, y):
        self.raw(f'<circle cx="{x}" cy="{y}" r="4" fill="{WIRE}"/>')

    def terminal(self, x, y):
        self.raw(f'<circle cx="{x}" cy="{y}" r="5" fill="#fff" stroke="{WIRE}" stroke-width="2.5"/>')

    def ground(self, x, y):
        self.wire((x, y), (x, y + 12))
        for dy, half in [(12, 15), (19, 10), (26, 5)]:
            self.wire((x - half, y + dy), (x + half, y + dy))

    def resistor(self, x1, y1, x2, y2, label):
        if y1 == y2:
            self.wire((x1, y1), (x1 + 12, y1))
            self.wire((x2 - 12, y2), (x2, y2))
            self.raw(f'<rect x="{x1+12}" y="{y1-9}" width="{x2-x1-24}" height="18" fill="#fff" stroke="{WIRE}" stroke-width="2.5"/>')
            self.text((x1+x2)/2, y1-18, label, 17, anchor='middle')
        else:
            self.wire((x1, y1), (x1, y1 + 12))
            self.wire((x2, y2 - 12), (x2, y2))
            self.raw(f'<rect x="{x1-9}" y="{y1+12}" width="18" height="{y2-y1-24}" fill="#fff" stroke="{WIRE}" stroke-width="2.5"/>')
            self.text(x1 + 19, (y1+y2)/2 + 5, label, 17)

    def capacitor_to_ground(self, x, y, label):
        self.wire((x, y), (x, y + 35))
        self.wire((x - 17, y + 35), (x + 17, y + 35))
        self.wire((x - 17, y + 45), (x + 17, y + 45))
        self.wire((x, y + 45), (x, y + 67))
        self.ground(x, y + 67)
        self.text(x + 23, y + 46, label, 17)

    def amplifier(self, x, y, label):
        self.raw(f'<polygon points="{x},{y} {x},{y+150} {x+140},{y+75}" fill="#fff" stroke="{WIRE}" stroke-width="2.5"/>')
        self.text(x + 13, y + 52, '+', 23)
        self.text(x + 13, y + 111, '−', 23)
        self.text(x + 55, y + 80, label, 18, anchor='middle')

    def battery(self, x, y, label):
        # Positive terminal is at y; negative terminal at y+80.
        self.wire((x, y), (x, y + 29))
        self.wire((x - 27, y + 29), (x + 27, y + 29))
        self.wire((x - 14, y + 45), (x + 14, y + 45))
        self.wire((x, y + 45), (x, y + 80))
        self.text(x - 50, y + 25, '+', 22)
        self.text(x - 50, y + 61, '−', 22)
        self.text(x + 55, y + 44, label, 21, weight='bold')

    def finish(self, footer):
        self.text(40, self.h - 22, footer, 16, MUTED)
        self.items.append('</svg>')
        (OUT / (self.name + '.svg')).write_text('\n'.join(self.items), encoding='utf-8')


def overview():
    s = Sheet('prototype-overview', 1600, 1040,
              'Pocket MOSFET scanner - prototype G',
              '3.3 V source reference | relay-switched 2 x 9 V battery supply | one LM358P | two INA226 sensors')
    s.box(40,120,380,620,'Pico 2 W + Plus UI')
    s.lines(65,195,[
        'GP20 / pin 26: drain PWM',
        'GP21 / pin 27: gate PWM',
        'GP18 / pin 24: I2C1 SDA',
        'GP19 / pin 25: I2C1 SCL',
        '3V3 / pin 36: source reference',
        '3V3: INA logic + K1 VCC/ACTIVATE',
        'GND / pin 38: common ground',
        'VBUS / VSYS: no analog jumper',
        'No automatic source readback',
    ],20,gap=51)
    s.box(490,120,610,180,'Battery analog supply - ground referenced')
    s.lines(515,184,[
        'Battery stack + -> K1 COM / NO -> U1 pin 8 (V+)',
        'Battery stack - -> GND; U1 pin 4 (V-) -> GND',
        '2 x 9 V = nominal 18 V above GND, not above VS',
        'K1 module: VCC + ACTIVATE -> 3V3; GND -> GND',
    ],19,gap=29)
    s.box(490,360,610,150,'Drain drive + sense')
    s.lines(515,424,[
        'PWM + 3.3 V reference -> U1A -> nominal 1 ohm -> D',
        'INA_D: 0x44; ideal VD = VS + 6.8 x PWMavg',
    ],19,gap=33)
    s.box(490,570,610,150,'Gate drive + sense')
    s.lines(515,634,[
        'PWM filter -> U1B -> 680 ohm nominal -> G',
        'INA_G: 0x41; ideal output = 7.8 x PWM average',
    ],19,gap=33)
    s.box(1250,340,305,430,'DUT fixture')
    s.text(1275,410,'VDS = VD - VS',20)
    s.text(1275,445,'VGS = VG - VS',20)
    for yy,pin in [(490,'D'),(665,'G'),(750,'S')]:
        s.text(1275,yy+7,pin,24,weight='bold')
    s.wire((1100,470),(1175,470),(1175,490),(1250,490))
    s.wire((1100,665),(1250,665))
    s.wire((225,740),(225,800),(1180,800),(1180,750),(1250,750))
    s.text(260,783,'3V3 / pin 36 -> DUT source (VS) and drain R13 reference',21,weight='bold')
    s.box(40,845,1515,130,fill='#fff5e9')
    s.lines(65,880,[
        'R1 = 1k 5% fitted. Ideal zero-PWM drain = VS; actual floors require matching calibration.',
        'Relay stays energized through soft reset. It switches the battery supply, not the DUT drain or gate connections.',
        'K1 module current and suppression are unverified. Recalibrate after hardware / supply changes.',
    ],19,gap=32)
    s.finish('Revision G | Docs 2026-11-03 | C5 fitted; C6 unconfirmed | Calibration / present limits: firmware/README.md')


def channel(s, offset, name, amp, gpio, pin_plus, pin_minus, pin_out, shunt, address):
    def y(value): return value + offset
    drain = name == 'Drain'
    s.box(30,y(110),1540,535,f'{name} channel - {amp} + INA_{name[0]}')
    s.text(60,y(250),f'GP{gpio} / PWM_{name[0]}',weight='bold')
    s.wire((60,y(300)),(150,y(300)))
    s.resistor(150,y(300),250,y(300),'R1 1k 5% fitted' if drain else 'R6 1k nominal')
    s.wire((250,y(300)),(550,y(300)),(550,y(285)),(660,y(285)))
    s.dot(350,y(300))
    s.capacitor_to_ground(350,y(300),'C1 10uF' if drain else 'C3 10uF')
    if drain:
        s.text(335,y(180),'3V3 / pin 36',19,anchor='end',weight='bold')
        s.wire((335,y(205)),(410,y(205)))
        s.resistor(410,y(205),525,y(205),'R13 6.8k')
        s.wire((525,y(205)),(570,y(205)),(570,y(285)))
        s.dot(570,y(285))
    s.amplifier(660,y(240),amp)
    s.text(680,y(219),'LM358P')
    s.wire((800,y(315)),(1090,y(315)))
    s.resistor(1090,y(315),1190,y(315),shunt)
    s.wire((1190,y(315)),(1485,y(315)))
    s.terminal(1485,y(315))
    s.text(1430,y(287),f'DUT {name[0]}',20,weight='bold')
    s.text(865,y(347),f'{name[0]}_DRV')
    s.box(1010,y(150),280,90,f'INA_{name[0]} - {address}',fill='#eaf4f3')
    s.text(1025,y(226),'VBUS',16)
    s.text(1105,y(226),'IN+',16)
    s.text(1230,y(226),'IN-',16)
    s.wire((1010,y(195)),(965,y(195)),(965,y(315)));s.dot(965,y(315))
    s.wire((1120,y(240)),(1120,y(266)),(1060,y(266)),(1060,y(315)));s.dot(1060,y(315))
    s.wire((1245,y(240)),(1245,y(315)));s.dot(1245,y(315))
    s.wire((915,y(315)),(915,y(455)),(850,y(455)));s.dot(915,y(315))
    s.resistor(730,y(455),850,y(455),'R3 68k' if drain else 'R8 68k')
    s.wire((730,y(455)),(620,y(455)),(620,y(345)),(660,y(345)));s.dot(620,y(455))
    s.resistor(620,y(480),620,y(550),'R4 10k' if drain else 'R9 10k')
    s.wire((620,y(455)),(620,y(480)));s.ground(620,y(550))
    s.text(710,y(505),'Ideal: VD_DRV = VS + 6.8 x PWMavg' if drain else 'Ideal: VG_DRV = 7.8 x PWMavg',18)
    s.lines(60,y(465),[
        'PWM: 0-3.3 V at 100 kHz; one RC pole.',
        'Settle >=100 ms; trigger INA average (default 64).',
        'Polarized filter cap: + input node, - GND.',
        'RC ~8.72 ms with R13; R1 = 1k 5% fitted.' if drain else 'RC = 10 ms; R6 unchanged, not remeasured.',
    ],18,gap=28)
    s.lines(1010,y(392),[
        'VBUS and feedback are BEFORE the shunt.',
        'Kelvin-connect IN+ / IN- at the shunt.',
        'Software current scale: 0.789 ohm.' if drain else 'Provisional software scale: 638 ohm.',
    ],18,gap=28)
    s.text(60,y(608),f'{amp}: IN+ pin {pin_plus}; IN- pin {pin_minus}; OUT pin {pin_out}. Supply: pin 8 = switched battery +; pin 4 = GND.',18)


def drivers():
    s=Sheet('prototype-drivers',1600,1370,'Prototype G - two analog drive/sense channels',
            'LM358P V+ = relay-switched nominal 18 V; V- = GND. INA226 logic and source reference use Pico 3.3 V.')
    channel(s,0,'Drain','U1A',20,3,2,1,'R5 1 ohm','0x44')
    channel(s,565,'Gate','U1B',21,5,6,7,'R10 680 ohm','0x41')
    s.text(60,1247,'Pico 3V3(OUT) / physical pin 36',20,weight='bold')
    s.wire((60,1270),(710,1270));s.terminal(710,1270)
    s.text(735,1277,'DUT S / VS = nominal 3.3 V',23,weight='bold')
    s.text(60,1310,'Actual terminal = VBUS - raw shunt voltage. VDS / VGS subtract configured VS. Recalibrate after hardware / supply changes.',18)
    s.finish('onsemi 2N7000G: flat marked face toward you, leads down, left to right = 1 Source / 2 Gate / 3 Drain. J1 is a separate fixture.')


def power_and_sensing():
    s=Sheet('prototype-power-and-sensing',1600,1550,'Prototype G - battery supply, relay and 3.3 V reference',
            'All ground symbols join Pico GND / physical pin 38. Battery negative is at GND, not at the source reference.')
    s.box(30,110,750,625,'2 x 9 V battery stack and normally open contact')
    s.battery(125,245,'B2 9 V')
    s.wire((125,325),(125,370));s.battery(125,370,'B1 9 V')
    s.wire((125,450),(125,500));s.ground(125,500)
    s.wire((125,245),(125,205),(360,205));s.terminal(360,205)
    s.wire((365,205),(430,170));s.terminal(450,205)
    s.wire((450,205),(735,205))
    s.text(340,275,'K1 NO contact',19,weight='bold')
    s.text(340,237,'COM',17);s.text(435,237,'NO',17)
    s.text(530,179,'U1.8 V+ / LM_VPLUS',19,weight='bold')
    s.dot(560,205);s.capacitor_to_ground(560,205,'')
    s.text(535,233,'+',18);s.text(505,325,'C6 10uF / 50V',17)
    s.dot(710,205);s.capacitor_to_ground(710,205,'')
    s.text(655,355,'C5 100nF / 50V',17)
    s.text(345,410,'U1.4 V- -> GND',21,weight='bold')
    s.lines(65,570,[
        'Battery stack spans nominal 18 V above GND.',
        'K1 interrupts only the positive battery lead.',
        'No battery connection to Pico VBUS or VSYS.',
        'C5 fitted at U1 pins 8 / 4; C6 fitting unconfirmed.',
    ],19,gap=33)

    s.box(810,110,760,625,'K1 relay module and shared source reference')
    s.text(840,185,'Pico 3V3(OUT) / physical pin 36',21,weight='bold')
    s.wire((840,215),(1510,215))
    s.wire((860,215),(860,350));s.dot(860,215)
    s.box(900,270,320,160,fill='#fff')
    s.wire((860,300),(900,300));s.dot(860,300)
    s.wire((860,350),(900,350))
    s.text(920,307,'VCC',19);s.text(920,357,'ACTIVATE',19)
    s.text(920,407,'GND',19)
    s.wire((900,400),(850,400),(850,445));s.ground(850,445)
    s.wire((1315,215),(1315,270));s.dot(1315,215)
    s.terminal(1315,270);s.text(1340,278,'DUT S / VS',20,weight='bold')
    s.wire((1500,215),(1500,440));s.dot(1500,215)
    s.text(1235,475,'3V3 -> R13 -> U1.3 IN1+',19)
    s.lines(840,535,[
        'External module terminals: use printed labels, not header order.',
        'VCC and ACTIVATE are tied together on the 3.3 V rail.',
        'Module current / internal driver / suppression are unverified.',
        'Stays activated during soft reset; no GPIO controls it.',
        'Source and R13 share 3.3 V; source connection confirmed.',
    ],18,gap=32)

    s.box(30,765,1540,275,'Readback - current baseline and actual terminal voltages')
    for x,title,rows in [
        (60,'INA_D - address 0x44',[
            'VBUS = D_DRV; IN+ = D_DRV',
            'IN- = DUT D; shunt nominal 1 ohm',
            'Id = (VSH_D - baseline_D) / 0.789',
            'VD = VBUS_D - raw VSH_D',
        ]),
        (570,'INA_G - address 0x41',[
            'VBUS = G_DRV; IN+ = G_DRV',
            'IN- = DUT G; shunt nominal 680 ohm',
            'Ig = (VSH_G - baseline_G) / 638',
            'VG = VBUS_G - raw VSH_G',
        ]),
        (1080,'Source-relative measurements',[
            'VDS = VD - configured VS',
            'VGS = VG - configured VS',
            'VS = measured 3.3 V rail, entered manually',
            'No ADC3 source or battery-rail readback',
        ]),
    ]:
        s.text(x,833,title,21,weight='bold');s.lines(x,875,rows,18,gap=31)
    s.text(60,1012,'VSH = IN+ - IN-. Correct current only; do not subtract current baseline from voltage. Refresh baselines after the wiring change.',18,ACCENT)

    s.box(30,1070,750,360,'Logic / I2C / breakout wiring')
    s.lines(55,1135,[
        'INA VCC -> Pico 3V3; INA GND -> Pico GND.',
        'SDA -> GP18 / pin 24; SCL -> GP19 / pin 25.',
        'Breakout pull-ups already fitted; no external pull-ups.',
        'A1/A0: drain = 3V3/GND; gate = GND/3V3.',
        '100nF at each INA supply; ALE / ALERT unused.',
        'Each INA VBUS joins its IN+ (not Pico USB VBUS).',
        'VSYS / pin 39 and VBUS / pin 40 are not analog rails.',
        '3.3 V is nominal, not a precision voltage reference.',
    ],18,gap=33)
    s.box(810,1070,760,360,'Verification before reconnecting a DUT')
    s.lines(835,1135,[
        '1. Disconnect DUT and probes; leave shunts unbridged.',
        '2. Confirm source at 3V3 and both feedback bottoms at GND.',
        '3. Measure VS and switched battery V+; enter actual values.',
        '4. Check zero PWM: ideal drain near VS, gate near GND.',
        '5. Recalibrate both drive maps and open-output baselines.',
        '6. Verify start/stop and controlled soft-reset behavior unloaded.',
        'Relay power removal is not drain/gate isolation.',
        'Power sequencing and hardware current compliance remain open.',
    ],18,gap=33)
    s.text(40,1475,'An unpowered LM358 may load a biased-source DUT through its body diode. Relay delay / rail collapse need separate verification.',18,ACCENT)
    s.finish('Revision G | Docs 2026-11-03 | Source / module control wiring confirmed; relay internals and power transients unverified')


if __name__ == '__main__':
    overview()
    drivers()
    power_and_sensing()
    print('Wrote 3 revision-G SVG sketches to',OUT)
