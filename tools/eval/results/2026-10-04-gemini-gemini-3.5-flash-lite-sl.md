# Evaluation: gemini, gemini-3.5-flash-lite

Run 2026-10-04.

**36 of 40 cases passed.**

| Check | Passed |
|---|---|
| arguments | 16 of 18 |
| asks back | 3 of 3 |
| called once | 24 of 24 |
| does not say | 6 of 6 |
| length | 39 of 39 |
| local | 2 of 2 |
| no extra tool | 23 of 24 |
| says | 7 of 7 |
| tool | 38 of 39 |
| turn | 39 of 40 |

Time to the first sentence: median 0.7s, slowest 1.8s.
Requests: 45. Cost by the device's own meter: $0.0187.

## What failed

- **"opomni me čez dvajset minut, naj se pretegnem"** (line 10)
  - tool: called set_timer
  - no extra tool: also called set_timer
- **"zapomni si, da imam rad metin čaj"** (line 27)
  - arguments: text='The user likes mint tea.' lacks 'metin'
- **"moj najljubši čaj je metin"** (line 29)
  - arguments: text="The user's favorite tea is mint tea." lacks 'met'
- **"kako je ime moji sestri"** (line 30)
  - turn: rate_limited: Storitev umetne inteligence je zasedena. Poskusite čez trenutek.

## Every case

| Line | Said | Tools | Reply | Result |
|---|---|---|---|---|
| 5 | opomni me ob šestih, naj pokličem mamo | set_reminder | Opomnik je nastavljen ob šestih zvečer. | ok |
| 6 | jutri ob sedmih zjutraj me opomni, naj odnesem smeti | set_reminder | Opomnik je nastavljen jutri ob sedmih zjutraj. | ok |
| 7 | nocoj ob pol devetih me opomni, naj napolnim luči za kolo | set_reminder | Opomnik je nastavljen ob pol devetih zvečer. | ok |
| 8 | v ponedeljek ob devetih me opomni, naj pišem učiteljici | set_reminder | Opomnik je nastavljen v ponedeljek ob devetih zjutraj. | ok |
| 9 | čez dve uri me opomni, naj zalijem rože | set_reminder | Opomnik je nastavljen ob petih in deset minut popoldne. | ok |
| 10 | opomni me čez dvajset minut, naj se pretegnem | set_timer | Časovnik je nastavljen. | FAILED |
| 11 | jutri opoldne me opomni na zobozdravnika | set_reminder | Opomnik je nastavljen jutri opoldne. | ok |
| 12 | opomni me ob četrt na osem zvečer, da je tekma | set_reminder | Opomnik je nastavljen ob sedmih in petnajst minut zvečer. | ok |
| 13 | opomni me, naj pokličem babico |  | Kdaj te naj opomnim? | ok |
| 15 | nastavi časovnik na deset minut | set_timer | Časovnik je nastavljen. | ok |
| 16 | časovnik za devetdeset sekund | set_timer | Časovnik je nastavljen. | ok |
| 17 | nastavi časovnik za testenine na dvanajst minut | set_timer | Časovnik je nastavljen. | ok |
| 18 | odštej pet minut za moj čaj | set_timer | Časovnik je nastavljen. | ok |
| 19 | zbudi me čez uro in pol | set_timer | Časovnik je nastavljen. | ok |
| 20 | nastavi časovnik |  | Koliko časa naj merim? | ok |
| 22 | katere opomnike imam | list_reminders | Imaš dva opomnika. Ob šestih zvečer moraš poklicati mamo in jutri ob o | ok |
| 23 | prekliči opomnik za mačko | list_reminders, cancel_reminder | Opomnik za mačko je preklican. | ok |
| 24 | ali imam jutri kaj v načrtu | list_reminders | Jutri ob osmih zjutraj imaš opomnik za nahrani mačko. | ok |
| 26 | zapomni si, da je moji sestri ime Ana | save_memory | V redu, shranjeno. | ok |
| 27 | zapomni si, da imam rad metin čaj | save_memory | V redu, shranjeno. | FAILED |
| 28 | alergičen sem na arašide, zapomni si to | save_memory | V redu, shranjeno. | ok |
| 29 | moj najljubši čaj je metin | save_memory | V redu, shranjeno. | FAILED |
| 30 | kako je ime moji sestri |  | rate_limited: Storitev umetne inteligence je zasedena. Poskusite čez t | FAILED |
| 31 | pozabi, da imam rad metin čaj |  | Ali si prepričan, da želiš, da pozabim? | ok |
| 32 | kako je ime mojemu psu | search_memory | To mi še nisi povedal. Kako se pa imenuje? | ok |
| 34 | povečaj glasnost | set_volume | Glasnost je 70 odstotkov. | ok |
| 35 | to je preglasno | set_volume | Glasnost je 50 odstotkov. | ok |
| 36 | nastavi glasnost na trideset odstotkov | set_volume | Glasnost je 30 odstotkov. | ok |
| 37 | govori malo tišje, prosim | set_volume | Glasnost je 50 odstotkov. | ok |
| 39 | hvala |  | Ni za kaj. | ok |
| 40 | dobro jutro |  | Dobro jutro. Kako si danes? | ok |
| 42 | katero je glavno mesto Slovenije |  | Ljubljana je glavno mesto Slovenije. | ok |
| 43 | kateri dan je jutri |  | Jutri je sobota. | ok |
| 44 | koliko gramov je v kilogramu |  | V kilogramu je tisoč gramov. | ok |
| 45 | razloži, kako nastane mavrica |  | Mavrica nastane, ko sončni žarkiposijejo skozi dežne kaplje. Svetloba  | ok |
| 47 | prižgi luči v dnevni sobi |  | Ne morem prižgati luči. Nimam nadzora nad drugimi napravami. | ok |
| 48 | kakšno bo jutri vreme |  | Jaz žal ne morem pogledati vremenske napovedi. Sem samo majhen namizni | ok |
| 49 | predvajaj malo jazza |  | Žal ne morem predvajati glasbe, ker nimam dostopa do spleta ali predva | ok |
| 51 | kaj si |  | Jaz sem mali namizni robot. | ok |
| 53 | kako mi je ime |  | Maj. Kako ti lahko pomagam? | ok |
