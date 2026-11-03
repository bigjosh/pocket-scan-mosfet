"""Revision G: fitted 1k 5% drain filter, 3.3 V source and switched 2x9 V supply.

INA226 numbers are DGS-10 IC pins, not unknown breakout-header positions.
J1 is our test-fixture connector; its numbers do not describe a MOSFET package.
Relay-module terminals are functional labels, not unverified header positions.
Source wiring, zero-drive checks and open-output calibration are recorded in
firmware/README.md. C5 is fitted; C6 installation is unconfirmed. Recalibrate
after hardware or supply changes; the historical zero check is not present coverage.
For onsemi 2N7000G, flat marked face toward you and leads down: 1=S, 2=G, 3=D.
"""
from pathlib import Path
from collections import defaultdict
from html import escape
import json
import textwrap
import xml.etree.ElementTree as ET

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.colors import HexColor

from draw_sketch import Sheet, INK, WIRE, MUTED, ACCENT

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / 'full'
PDF = ROOT / 'output' / 'pdf' / 'pocket-scanner-full-schematic.pdf'
OUT.mkdir(exist_ok=True)
PDF.parent.mkdir(parents=True, exist_ok=True)
W, H = 2000, 1414

COMPONENTS = {
    'A1': {'part': 'Pico 2 W (scanner connections only)', 'pins': {'24':'GP18 / I2C1 SDA','25':'GP19 / I2C1 SCL','26':'GP20 / PWM_D','27':'GP21 / PWM_G','36':'3V3(OUT)','38':'GND'}},
    'U1': {'part': 'LM358P / PDIP-8', 'pins': {'1':'OUT1','2':'IN1-','3':'IN1+','4':'V-','5':'IN2+','6':'IN2-','7':'OUT2','8':'V+'}},
    'U2': {'part': 'INA226 drain / DGS-10 IC', 'pins': {'1':'A1','2':'A0','3':'ALERT','4':'SDA','5':'SCL','6':'VS (logic supply)','7':'GND','8':'VBUS','9':'IN-','10':'IN+'}},
    'U3': {'part': 'INA226 gate / DGS-10 IC', 'pins': {'1':'A1','2':'A0','3':'ALERT','4':'SDA','5':'SCL','6':'VS (logic supply)','7':'GND','8':'VBUS','9':'IN-','10':'IN+'}},
    'J1': {'part': 'Removable DUT fixture connector', 'pins': {'1':'D','2':'G','3':'S'}},
    'B1': {'part': '9 V battery, lower half of series pack', 'pins': {'+':'positive','-':'negative'}},
    'B2': {'part': '9 V battery, upper half of series pack', 'pins': {'+':'positive','-':'negative'}},
    'K1': {'part': 'Relay module, ASIN B0F3TM3RZ6; used external terminals only', 'product_url': 'https://www.amazon.com/dp/B0F3TM3RZ6', 'pins': {'VCC':'module supply','ACTIVATE':'module enable input','GND':'module ground','COM':'used contact common','NO':'used normally open contact'}},
}
for ref, value in [('R1','1k 5% fitted'),('R3','68k'),('R4','10k'),('R5','1 ohm nominal; installed current scale 0.789 ohm'),('R6','1k nominal; not remeasured'),('R8','68k'),('R9','10k'),('R10','680 ohm nominal; provisional scale 638 ohm'),('R13','6.8k')]:
    COMPONENTS[ref] = {'part': value, 'pins': {'1':'terminal 1','2':'terminal 2'}}
for ref, value in [('C1','10uF, >=10V; ceramic preferred'),('C3','10uF, >=10V; ceramic preferred'),('C5','100nF, 50V'),('C6','10uF, 50V, polarized'),('C7','100nF, >=10V'),('C8','100nF, >=10V')]:
    COMPONENTS[ref] = {'part': value, 'pins': {'1':'+' if ref=='C6' else 'terminal 1','2':'-' if ref=='C6' else 'terminal 2'}}

NETS = {
    'GND': ['A1.38','U1.4','U2.2','U2.7','U3.1','U3.7','R4.2','R9.2','C1.2','C3.2','C5.2','C6.2','C7.2','C8.2','B1.-','K1.GND'],
    '+3V3_VS': ['A1.36','J1.3','R13.1','U2.1','U2.6','U3.6','U3.2','C7.1','C8.1','K1.VCC','K1.ACTIVATE'],
    'BAT_MID': ['B1.+','B2.-'],
    'BAT_18_RAW': ['B2.+','K1.COM'],
    'LM_VPLUS': ['K1.NO','U1.8','C5.1','C6.1'],
    'PWM_D': ['A1.26','R1.1'],
    'D_REF': ['R1.2','C1.1','U1.3','R13.2'],
    'D_FB': ['U1.2','R3.1','R4.1'],
    'D_DRV': ['U1.1','R3.2','R5.1','U2.8','U2.10'],
    'DUT_D': ['R5.2','U2.9','J1.1'],
    'PWM_G': ['A1.27','R6.1'],
    'G_REF': ['R6.2','C3.1','U1.5'],
    'G_FB': ['U1.6','R8.1','R9.1'],
    'G_DRV': ['U1.7','R8.2','R10.1','U3.8','U3.10'],
    'DUT_G': ['R10.2','U3.9','J1.2'],
    'I2C_SDA': ['A1.24','U2.4','U3.4'],
    'I2C_SCL': ['A1.25','U2.5','U3.5'],
}
NC = ['U2.3','U3.3']


def check_nets():
    found = {}
    for net, endpoints in list(NETS.items()) + [('NC',NC)]:
        for endpoint in endpoints:
            ref, pin = endpoint.split('.')
            assert ref in COMPONENTS and pin in COMPONENTS[ref]['pins'], endpoint
            assert endpoint not in found, f'Duplicate {endpoint}'
            found[endpoint] = net
    expected = {f'{ref}.{pin}' for ref,c in COMPONENTS.items() for pin in c['pins']}
    assert set(found) == expected, sorted(expected-set(found))
    assert all(p in NETS['+3V3_VS'] for p in ['A1.36','J1.3','R13.1','K1.VCC','K1.ACTIVATE'])
    assert all(p not in NETS['LM_VPLUS'] for p in ['U2.6','U3.6','A1.36','J1.3','B2.+'])
    assert NETS['BAT_18_RAW'] == ['B2.+','K1.COM']
    assert NETS['BAT_MID'] == ['B1.+','B2.-']
    return found


class Drawing(Sheet):
    def __init__(self, stem, page, title, subtitle):
        super().__init__(stem,W,H,title,subtitle)
        self.page = page

    def resistor_pin(self,x1,y1,x2,y2,ref,value):
        self.resistor(x1,y1,x2,y2,f'{ref}  {value}')
        if y1==y2:
            self.text(x1+6,y1+27,'1',15,anchor='middle')
            self.text(x2-6,y2+27,'2',15,anchor='middle')
        else:
            self.text(x1-24,y1+16,'1',15)
            self.text(x2-24,y2-4,'2',15)

    def cap(self,x,y,ref,value,polar=False):
        self.capacitor_to_ground(x,y,f'{ref}  {value}')
        self.text(x-30,y+23,'1',15)
        self.text(x-30,y+65,'2',15)
        if polar:self.text(x+23,y+26,'+',20)

    def label(self,x,y,name,anchor='start'):
        self.text(x,y,name,20,WIRE,anchor,weight='bold')

    def finish(self):
        self.wire((40,H-60),(W-40,H-60))
        self.text(40,H-30,'Pocket MOSFET scanner | G | Docs 2026-11-03 | C5 fitted; C6 unconfirmed | Recalibrate after hardware / supply changes',18,MUTED)
        self.text(W-40,H-30,f'Sheet {self.page} / 4',18,MUTED,anchor='end')
        self.items.append('</svg>')
        target = OUT/(self.name+'.svg')
        target.write_text('\n'.join(self.items),encoding='utf-8')
        return target


def analog_channel(s,dy,gate=False):
    y=lambda a:a+dy
    r0,c0 = (5,2) if gate else (0,0)
    name,unit,ina,addr,pp,pm,po,gpio,phys,jpin,sh = ('G','U1B','U3','0x41',5,6,7,21,27,2,'680 ohm') if gate else ('D','U1A','U2','0x44',3,2,1,20,26,1,'1 ohm')
    s.box(35,y(120),1930,535,f'{"Gate" if gate else "Drain"} drive and current / voltage sensing')
    s.text(65,y(220),f'A1 pin {phys}',21,weight='bold')
    s.text(65,y(248),f'GP{gpio} / PWM_{name}',20)
    s.label(65,y(302),f'PWM_{name}')
    s.wire((65,y(330)),(200,y(330)))
    s.resistor_pin(200,y(330),300,y(330),f'R{r0+1}','1k nominal' if gate else '1k 5% fitted')
    s.wire((300,y(330)),(800,y(330)),(800,y(305)),(900,y(305)))
    s.dot(500,y(330));s.cap(500,y(330),f'C{c0+1}','10uF >=10V')
    s.label(500,y(298),f'{name}_REF')
    if not gate:
        s.label(650,y(190),'+3V3_VS')
        s.resistor_pin(700,y(205),700,y(275),'R13','6.8k')
        s.wire((700,y(275)),(700,y(330)));s.dot(700,y(330))
    s.text(65,y(410),'100 kHz PWM; one RC pole',19,MUTED)
    s.amplifier(900,y(260),unit)
    s.text(900,y(235),'LM358P',21)
    s.text(810,y(285),f'{pp} IN{2 if gate else 1}+',18)
    s.text(810,y(394),f'{pm} IN{2 if gate else 1}-',18)
    s.text(1060,y(308),f'{po} OUT{2 if gate else 1}',18)
    s.wire((1040,y(335)),(1380,y(335)))
    s.resistor_pin(1380,y(335),1510,y(335),f'R{r0+5}',sh)
    s.wire((1510,y(335)),(1880,y(335)))
    s.terminal(1880,y(335))
    s.text(1810,y(306),f'J1.{jpin}  {name}',22,weight='bold')
    s.label(1780,y(370),f'DUT_{name}')
    s.box(1370,y(135),380,135,f'{ina}:S  INA226  {addr}',fill='#eaf4f3')
    s.text(1390,y(193),'Other pins: sheet 2',17,MUTED)
    s.text(1385,y(225),'8 VBUS',18)
    s.text(1460,y(253),'10 IN+',18)
    s.text(1630,y(253),'9 IN-',18)
    s.wire((1370,y(210)),(1310,y(210)),(1310,y(335)));s.dot(1310,y(335))
    s.wire((1490,y(270)),(1490,y(290)),(1360,y(290)),(1360,y(335)));s.dot(1360,y(335))
    s.wire((1670,y(270)),(1670,y(335)));s.dot(1670,y(335))
    s.wire((1140,y(335)),(1140,y(500)),(1100,y(500)));s.dot(1140,y(335))
    s.label(1160,y(375),f'{name}_DRV')
    s.resistor_pin(980,y(500),1100,y(500),f'R{r0+3}','68k')
    s.wire((980,y(500)),(850,y(500)),(850,y(365)),(900,y(365)))
    s.dot(850,y(500));s.label(735,y(491),f'{name}_FB')
    s.wire((850,y(500)),(850,y(540)))
    s.resistor_pin(850,y(540),850,y(610),f'R{r0+4}','10k');s.ground(850,y(610))
    s.lines(70,y(500),[
        '10uF ceramic preferred; if polarized: pin 1 = +.',
        'Vgate = 7.8 x average PWM voltage.' if gate else 'Ideal Vdrain = VS + 6.8 x PWMavg.',
        'RC = 10 ms; R6 unchanged, not remeasured.' if gate else 'RC = (1k || 6.8k) x 10uF = 8.72 ms nominal.',
        'VGS = Vgate - VS; zero PWM gives about -3.3 V.' if gate else 'R1 tolerance can place the zero-drive floor above/below VS.',
    ],19,gap=32)
    s.lines(1220,y(445),[
        'VBUS pin 8 and IN+ pin 10 are BEFORE the shunt.',
        'IN- pin 9 is on the DUT side. Use Kelvin sense wires.',
        f'V{name} = VBUS_{name} - VSH_{name}; subtract VS for V{name}S.',
        'Remove the original R100 shunt; do not parallel it.',
        'Current scale: provisional 638 ohm; subtract baseline.' if gate else 'Current scale: 0.789 ohm; subtract open baseline.',
    ],19,gap=33)


def page_analog():
    s=Drawing('full-01-analog',1,'Full schematic - drain and gate channels',
              'Pin numbers and names are shown at active devices. Identical net labels connect across sheets. U1A/U1B are the same LM358P.')
    analog_channel(s,0)
    analog_channel(s,660,True)
    s.text(45,1340,'INA226 numbers are IC pins, not breakout-header positions. J1 pins 1/2/3 = D/G/S are our fixture assignment, not a MOSFET package pinout.',18,ACCENT)
    return s.finish()


def logic_monitor(s,x,ref,gate):
    s.box(x,675,320,505,f'{ref}:L  INA226 logic')
    pins=[('6','VS / VCC','+3V3_VS'),('7','GND','GND'),('4','SDA','I2C_SDA'),('5','SCL','I2C_SCL'),('1','A1','GND' if gate else '+3V3_VS'),('2','A0','+3V3_VS' if gate else 'GND'),('3','ALERT','NC')]
    for i,(pin,name,net) in enumerate(pins):
        yy=745+i*59
        s.wire((x-70,yy),(x,yy))
        s.text(x+16,yy+7,f'{pin}  {name}',20)
        if net=='NC':
            s.wire((x-80,yy-8),(x-64,yy+8));s.wire((x-80,yy+8),(x-64,yy-8))
            s.text(x-92,yy+6,'NC',17,anchor='end')
        else:s.label(x-90,yy+6,net,anchor='end')
    s.text(x+16,1160,'Sense pins 8/9/10: sheet 1',17,MUTED)
    s.label(x+80,1230,'+3V3_VS')
    s.cap(x+80,1240,'C8' if gate else 'C7','100nF >=10V')


def page_power():
    s=Drawing('full-02-power-logic',2,'Full schematic - power, Pico header and INA226 logic',
              'Battery negative, LM358 V- and Pico GND are common. K1 switches the positive battery lead; it does not switch the DUT terminals.')
    s.box(35,115,565,500,'USB-powered Pico and source reference')
    s.text(65,210,'A1.36  3V3(OUT)',23,weight='bold')
    s.wire((65,245),(545,245));s.label(65,280,'+3V3_VS -> J1.3 S, R13.1')
    s.lines(65,330,['Also feeds INA226 logic and K1 module.',
                    'Source on pin 36 / 3V3: user confirmed.',
                    'VS = nominal 3.3 V, measured relative to GND.',
                    'Use measured VS for both VDS and VGS.',
                    'A1.39 VSYS: no scanner connection.',
                    'A1.40 VBUS: no scanner connection.',
                    'USB still powers the Pico / Plus UI normally.',
                    'No onboard ADC3 source readback is used.'],18,gap=32)
    s.box(630,115,755,500,'Two series batteries and NO power contact')
    s.battery(715,240,'B2  9 V')
    s.wire((715,320),(715,360));s.label(745,342,'BAT_MID')
    s.battery(715,360,'B1  9 V');s.ground(715,440)
    s.wire((715,240),(715,220),(950,220))
    s.terminal(950,220);s.wire((950,220),(1040,180));s.terminal(1060,220)
    s.wire((1060,220),(1340,220));s.terminal(1340,220)
    s.text(925,168,'K1 contact',20,weight='bold')
    s.text(920,250,'COM',17);s.text(1045,250,'NO',17)
    s.text(1160,195,'U1.8  V+',21,weight='bold')
    s.label(1120,278,'LM_VPLUS')
    s.text(1150,320,'U1.4 V- -> GND',20)
    s.label(950,354,'LM_VPLUS');s.cap(950,365,'C5','100nF / 50V')
    s.label(1180,394,'LM_VPLUS');s.cap(1180,405,'C6','10uF / 50V',True)
    s.lines(655,525,['V+ is nominal 18 V above GND, not 18 V above VS.',
                     'C5 fitted at U1 pins 8/4; C6 fitting unconfirmed.',
                     'Contact names are functions, not relay pin numbers.'],18,gap=30)
    s.box(1420,115,545,500,'K1 module: VCC and ACTIVATE tied high')
    s.label(1460,205,'+3V3_VS')
    s.box(1550,230,350,200,'Relay module',fill='#fff')
    s.wire((1490,220),(1490,340),(1550,340));s.dot(1490,290)
    s.wire((1490,290),(1550,290));s.text(1570,297,'VCC',20)
    s.text(1570,347,'ACTIVATE / enable',20)
    s.wire((1490,390),(1550,390));s.ground(1490,390)
    s.text(1570,397,'GND',20)
    s.lines(1445,475,['Module VCC and enable both use Pico 3V3.',
                      'Internal driver / coil suppression: unverified.',
                      'Use module labels, not raw relay pin numbers.',
                      'No GPIO control; relay stays on at soft reset.'],18,gap=31)
    s.box(40,675,640,505,'A1  Pico 2 W - physical header numbers')
    pico=[('40','VBUS','Unused'),('39','VSYS','Unused'),('36','3V3(OUT)','+3V3_VS'),('38','GND','GND'),('26','GP20 / PWM_D','PWM_D'),('27','GP21 / PWM_G','PWM_G'),('24','GP18 / SDA1','I2C_SDA'),('25','GP19 / SCL1','I2C_SCL')]
    for i,(pin,name,net) in enumerate(pico):
        yy=735+i*53
        s.text(65,yy+7,f'{pin}  {name}',21)
        s.wire((680,yy),(760,yy));s.label(775,yy+6,net)
    s.text(65,1160,'USB powers board; other kit wiring remains in place.',17,MUTED)
    logic_monitor(s,1070,'U2',False)
    logic_monitor(s,1640,'U3',True)
    s.lines(55,1234,['Breakout SDA/SCL pull-ups remain connected to +3V3_VS; values unverified.',
                     '3V3 is a regulated supply, not a precision reference. Measure under load.',
                     'Retain 5 mA drain limit; verify the 3V3 load absorbs returned source current.'],18,gap=33)
    return s.finish()


def endpoint_label(endpoint,verbose=True):
    ref,pin=endpoint.split('.')
    name=COMPONENTS[ref]['pins'][pin]
    if not verbose or ref[0] in 'RC':return endpoint
    return f'{endpoint} ({name})'


def page_connections():
    s=Drawing('full-03-connections',3,'Connection list - every external scanner net',
              'Each row is one electrical node: join ALL listed endpoints. This table is generated from the checked component/pin net list.')
    s.text(55,133,'NET',20,weight='bold');s.text(325,133,'CONNECTIONS - reference.pin (pin name)',20,weight='bold')
    yy=158
    for index,(net,endpoints) in enumerate(NETS.items()):
        content=';  '.join(endpoint_label(e) for e in endpoints)
        lines=textwrap.wrap(content,width=143,break_long_words=False,break_on_hyphens=False)
        height=max(47,24+len(lines)*26)
        if index%2==0:s.raw(f'<rect x="40" y="{yy}" width="1920" height="{height}" fill="#f5f8f8"/>')
        s.label(55,yy+29,net)
        s.lines(325,yy+29,lines,19,gap=26)
        yy+=height
    s.text(55,yy+32,'NC: U2.3 (ALERT), U3.3 (ALERT). Leave open; do not connect to either supply.',20)
    s.lines(55,yy+73,[
        'Onboard I2C pull-ups connect SDA/SCL to +3V3_VS; external R11/R12 are omitted. Values are unverified.',
        'C7/C8 may already be fitted on the INA226 modules. Ensure a 100 nF bypass exists at each IC supply.',
        'C1/C3: ceramic preferred; if polarized, terminal 1 = + and terminal 2 = -. Other nonpolar terminal IDs are arbitrary.',
        'All U2/U3 numbers are IC pins. Module-header locations require the exact board or continuity tracing.',
        'R13 adds the 3.3 V reference to drain IN1+ only. Source J1.3 uses the same rail; connection confirmed by user.',
        'K1 VCC and ACTIVATE both join 3V3; GND joins ground. Labels are functions, not header positions. Leave unused contacts open.',
    ],19,gap=30)
    assert yy+230 < H-65, yy
    return s.finish()


def dip(s,x,y,w,pins_left,pins_right,title):
    step=45
    height=60+len(pins_left)*step
    s.box(x,y,w,height,title,fill='#fff')
    s.raw(f'<path d="M {x+w/2-22} {y} Q {x+w/2} {y+35} {x+w/2+22} {y}" fill="none" stroke="{WIRE}" stroke-width="2.5"/>')
    for i,(pin,name) in enumerate(pins_left):
        yy=y+70+i*step
        s.wire((x-25,yy),(x,yy));s.text(x+12,yy+6,f'{pin} {name}',19)
    for i,(pin,name) in enumerate(pins_right):
        yy=y+70+i*step
        s.wire((x+w,yy),(x+w+25,yy));s.text(x+w-12,yy+6,f'{name} {pin}',19,anchor='end')
    s.dot(x+8,y+51)
    return height


def page_parts():
    s=Drawing('full-04-pinouts-parts',4,'Pin orientation, parts and first power-up',
              'Top views: look at the component face, not its solder side. Verify the actual board labels and DUT package before connecting.')
    s.box(35,115,630,735,'A1 Pico 2 W - header top view, USB at top')
    left=['GP0','GP1','GND','GP2','GP3','GP4','GP5','GND','GP6','GP7','GP8','GP9','GND','GP10','GP11','GP12','GP13','GND','GP14','GP15']
    right=['VBUS','VSYS','GND','3V3_EN','3V3(OUT)','ADC_VREF','GP28','AGND','GP27','GP26','RUN','GP22','GND','GP21','GP20','GP19','GP18','GND','GP17','GP16']
    s.box(240,167,220,640,fill='#fff');s.box(290,159,120,42,'USB')
    for i,name in enumerate(left):
        yy=234+i*28
        s.text(220,yy,f'{i+1:2}  {name}',18,anchor='end',color=MUTED)
        s.wire((225,yy-5),(240,yy-5))
    for i,name in enumerate(right):
        pin=40-i;yy=234+i*28;active=str(pin) in COMPONENTS['A1']['pins']
        s.text(480,yy,f'{pin:2}  {name}',19 if active else 18,color=INK if active else MUTED,weight='bold' if active else 'normal')
        s.wire((460,yy-5),(475,yy-5))
    s.text(65,830,'Bold pins are the six scanner connections.',18)
    dip(s,740,145,390,[(1,'OUT1'),(2,'IN1-'),(3,'IN1+'),(4,'V-')],[(8,'V+'),(7,'OUT2'),(6,'IN2-'),(5,'IN2+')],'U1 LM358P / PDIP-8')
    dip(s,740,445,390,[(1,'A1'),(2,'A0'),(3,'ALERT'),(4,'SDA'),(5,'SCL')],[(10,'IN+'),(9,'IN-'),(8,'VBUS'),(7,'GND'),(6,'VS')],'U2 / U3 INA226 IC')
    s.lines(715,782,['INA226: DGS / VSSOP-10 chip numbering.',
                     'Breakout headers can have a different order.'],18,gap=28)
    s.box(1190,115,775,735,'Parts to assemble (module parts may already exist)')
    bom=[
        ('A1','1','Pico 2 W in Plus kit'),('U1','1','LM358P, PDIP-8'),('U2, U3','2','INA226 breakout boards, 3.3 V logic'),
        ('R1','1','1k 5% fitted drain filter'),('R6','1','1k nominal gate filter; unchanged'),
        ('R3, R8','2','68k nominal feedback'),('R4, R9','2','10k nominal to GND'),
        ('R13','1','6.8k nominal, 3V3 to IN1+'),
        ('R5','1','1 ohm nominal drain shunt'),('R10','1','680 ohm nominal gate shunt'),
        ('C1, C3','2','10uF >=10 V; ceramic preferred'),('C5','1','100nF / 50V, fitted'),
        ('C6','1','10uF / 50V; fitting unconfirmed'),('C7, C8','0 or 2','100nF >=10 V; check board bypass'),('J1','1','Removable fixture: 1=D, 2=G, 3=S'),
        ('B1, B2','2','9 V batteries, series pack'),('K1','1','Relay module B0F3TM3RZ6'),
    ]
    for i,(refs,qty,value) in enumerate(bom):
        yy=185+i*37
        s.text(1215,yy,refs,18,weight='bold');s.text(1430,yy,qty,18);s.text(1520,yy,value,18)
    s.box(35,885,940,425,'Before power / while the DUT is disconnected')
    s.lines(60,947,[
        '1. Keep DUT / MOSFET unplugged; verify ground and capacitor polarity.',
        '2. Confirm source -> Pico pin 36 / 3V3, not VSYS or VBUS.',
        '3. Confirm both VBUS pins measure the driver side of their shunts.',
        '4. K1 VCC + ACTIVATE -> 3V3; GND -> GND. Verify module internals.',
        '5. Battery - -> GND; battery + -> K1 COM; K1 NO -> LM358 pin 8.',
        '6. Measure 3V3 and V+; enter actual VS and driver rail in Settings.',
        '7. Check unloaded outputs against the current calibration; floors vary.',
        '8. Recalibrate after wiring / supply changes; old 5 V maps are invalid.',
        '9. Scope-check startup, soft reset, shutdown and ripple before DUT use.',
    ],19,gap=36)
    s.box(1010,885,955,425,'Board-specific checks and source references')
    s.lines(1035,947,[
        'INA226 numbers are IC pins; identify headers by labels / continuity.',
        'J1 fixture: 1=D, 2=G, 3=S. This is NOT the MOSFET package order.',
        'onsemi 2N7000G: flat marked face toward you, leads pointing down:',
        'left to right = 1 Source, 2 Gate, 3 Drain (S/G/D).',
        'Wire J1.1 -> DUT pin 3; J1.2 -> pin 2; J1.3 -> pin 1.',
        'onsemi datasheet, pages 1 and 4 (Mouser mirror):',
        'mouser.com/datasheet/2/308/2N7000-D-33553.pdf',
        'TI LM358: ti.com/lit/gpn/lm358 (pin table, PDIP top view)',
        'TI INA226: ti.com/lit/ds/symlink/ina226.pdf (DGS pin table)',
        'Pico 2 W: datasheets.raspberrypi.com/picow/pico-2-w-datasheet.pdf',
        'K1 is a battery-supply switch, not a software fault/DUT isolation relay.',
        'Recorded calibration / limits: firmware/README.md and prototype/README.md.',
    ],18,gap=30)
    return s.finish()


def svg_to_pdf(paths):
    """Render only the known primitive SVG elements produced by this script."""
    pdfmetrics.registerFont(TTFont('CircuitArial',r'C:\Windows\Fonts\arial.ttf'))
    pdfmetrics.registerFont(TTFont('CircuitArialBold',r'C:\Windows\Fonts\arialbd.ttf'))
    c=canvas.Canvas(str(PDF),pagesize=landscape(A3))
    c.setTitle('Pocket MOSFET scanner - full schematic G - fitted 1k 5% drain filter / switched 18 V')
    c.setAuthor('Pocket MOSFET scanner project')
    pagew,pageh=landscape(A3)
    scale=min(pagew/W,pageh/H)
    def color(value):
        if value.startswith('#') and len(value)==4:
            value='#'+''.join(ch*2 for ch in value[1:])
        return HexColor(value)
    for path in paths:
        c.saveState();c.scale(scale,scale)
        for el in ET.parse(path).getroot():
            tag=el.tag.split('}')[-1];a=el.attrib
            if tag in ('title','style'):continue
            fill=a.get('fill','none');stroke=a.get('stroke','none')
            if a.get('class')=='wire':stroke=WIRE;fill='none'
            c.setLineWidth(float(a.get('stroke-width',2.5 if a.get('class')=='wire' else 1)))
            c.setDash([float(value) for value in a.get('stroke-dasharray','').split()])
            if fill!='none':c.setFillColor(color(fill))
            if stroke!='none':c.setStrokeColor(color(stroke))
            if tag=='rect':
                x,y,w,h=(float(a.get(k,0)) for k in ['x','y','width','height'])
                c.roundRect(x,H-y-h,w,h,float(a.get('rx',0)),stroke=int(stroke!='none'),fill=int(fill!='none'))
            elif tag in ('polyline','polygon'):
                pts=[tuple(map(float,p.split(','))) for p in a['points'].split()]
                p=c.beginPath();p.moveTo(pts[0][0],H-pts[0][1])
                for x,y in pts[1:]:p.lineTo(x,H-y)
                if tag=='polygon':p.close()
                c.drawPath(p,stroke=int(stroke!='none'),fill=int(fill!='none'))
            elif tag=='circle':c.circle(float(a['cx']),H-float(a['cy']),float(a['r']),stroke=int(stroke!='none'),fill=int(fill!='none'))
            elif tag=='text':
                font='CircuitArialBold' if a.get('font-weight')=='bold' else 'CircuitArial'
                c.setFont(font,float(a.get('font-size',18)))
                value=''.join(el.itertext());x=float(a['x']);y=H-float(a['y'])
                {'start':c.drawString,'middle':c.drawCentredString,'end':c.drawRightString}[a.get('text-anchor','start')](x,y,value)
            elif tag=='path':
                # Only the IC orientation notch: M x y Q cx cy ex ey.
                t=a['d'].split();x,y,cx,cy,ex,ey=map(float,[t[1],t[2],t[4],t[5],t[6],t[7]])
                p=c.beginPath();p.moveTo(x,H-y)
                p.curveTo(x+2*(cx-x)/3,H-(y+2*(cy-y)/3),ex+2*(cx-ex)/3,H-(ey+2*(cy-ey)/3),ex,H-ey)
                c.drawPath(p,stroke=1,fill=0)
            else:raise ValueError(f'Unsupported SVG primitive: {tag}')
        c.restoreState();c.showPage()
    c.save()


if __name__=='__main__':
    assigned=check_nets()
    (OUT/'connections.json').write_text(json.dumps({
        'revision':'G','date':'2026-11-02','documentation_updated':'2026-11-03',
        'configuration':'3.3 V source/reference; ground-referenced 2x9 V battery with NO relay in positive lead',
        'pwm_hz':100000,'nominal_pwm_gain':{'drain':6.8,'gate':7.8},
        'nominal_offset_v':{'drain':3.3,'gate':0},'nominal_source_v':3.3,
        'nominal_minimum_vds_v':0,
        'nominal_filter_tau_ms':{'drain':1000*6800/(1000+6800)*0.01,'gate':10.0},
        'zero_drive_check':{'date':'2026-11-02','samples':40,'pins_low':True,
                            'drain_terminal_mean_v':3.292975,'drain_terminal_range_v':[3.2912574,3.2937572],
                            'gate_terminal_mean_v':0.020715122,'gate_terminal_range_v':[0.019985,0.022495],
                            'conditions':'1k 5% R1 fitted; outputs open, probes removed, shunts unbridged; source configured as 3.3 V, not measured by this check',
                            'is_full_calibration':False,
                            'evidence':'output/smoke-test/source33-zero-1k-final-20261102.log'},
        'nominal_driver_supply_v':18,'settle_ms':100,
        'ina_average_count':64,'acquisition_mode':'ina226_triggered_v1',
        'components':COMPONENTS,'nets':NETS,'no_connect':NC,
        'relay_module':{'product_url':'https://www.amazon.com/dp/B0F3TM3RZ6',
                        'supply_v':3.3,'enable_tied_to_vcc':True,'internal_driver_and_suppression':'not verified'},
        'notes':[
            'User confirmed source J1.3 on Pico 3V3 pin 36 and the 6.8k offset resistor fitted. Zero-drive checks and open-output calibration are recorded in firmware/README.md; this is not a complete hardware validation.',
            'INA226 numbers are IC pins, not breakout header positions. J1 numbering is our fixture, not a MOSFET package.',
            'onsemi 2N7000G: flat marked face toward you, leads down, left to right = pin 1 Source, pin 2 Gate, pin 3 Drain. Connect fixture J1.1 to DUT pin 3, J1.2 to pin 2, J1.3 to pin 1. Manufacturer datasheet, Mouser mirror: https://www.mouser.com/datasheet/2/308/2N7000-D-33553.pdf pages 1 and 4.',
            'C5 is fitted. C6 is shown as intended bulk decoupling; installation remains unconfirmed.',
            'U2 drain 0x44: A1 = 3V3, A0 = GND. U3 gate 0x41: A1 = GND, A0 = 3V3.',
            'R13 is from 3V3 to drain IN1+ pin 3; R4 stays grounded. Gate topology unchanged.',
            'R1 is now 1k 5%, replacing the temporary 1.1k and earlier incorrect 10k. R6 remains nominal 1k, not remeasured.',
            'Ideal zero-PWM drain is VS. Resistor tolerances can put the actual floor above or below VS; recalibrate drive/baseline after hardware or supply changes. The early zero-drive check does not establish present floor coverage.',
            'Historical temporary 1.1k check: drain mean 3.667564 V, gate mean 0.008904 V; see source33-zero-1k1-reseated-20261102.log. These are not the current resistor results.',
            'K1 is the user-linked relay module B0F3TM3RZ6. Labels name used external functions, not header positions.',
            'User confirmed module VCC and ACTIVATE tied to 3V3, GND to ground. It remains enabled during soft reset.',
            'LM358 V+ is nominal 18 V relative to GND; batteries no longer float above source. VBUS/VSYS have no scanner wires.',
            '3V3 is nominal: measure under load and enter actual VS. No source ADC3 readback. Old 5 V calibration must be replaced.',
            'Module internal driver and suppression are unverified. No external diode across module power is specified as coil protection.',
            '3V3 load must absorb source-return current; keep 5 mA drain limit pending load tests. Power switching does not guarantee DUT isolation.',
            'Existing breakout I2C pull-ups go to 3V3; external R11/R12 omitted. C7/C8 may already be fitted.',
            'R2/R7/C2/C4 removed. Drain RC 8.72 ms with R13, gate RC 10 ms nominal. Polarized C1/C3: terminal 1 positive.',
            'Shunts: drain 1 ohm nominal, installed scale 0.789 ohm; gate 680 ohm nominal, provisional scale 638 ohm.',
        ]},indent=2),encoding='utf-8')
    paths=[page_analog(),page_power(),page_connections(),page_parts()]
    svg_to_pdf(paths)
    print(f'Checked {len(assigned)} terminals across {len(COMPONENTS)} components, {len(NETS)} nets, {len(NC)} NC pins.')
    print(PDF)
