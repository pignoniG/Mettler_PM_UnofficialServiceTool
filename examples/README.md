# Example dumps

Four factory EEPROMs read from working balances, with the numbers printed on their parameter stickers.
They serve two purposes: as reference data for anyone decoding their own dump, and as **templates** when
programming a blank EEPROM - start from the dump of the same model, then load your own sticker over it
(see the main README).

All four files are in the byte order produced by the programmer they were read with (word bytes swapped
relative to the balance's processor); the tool detects that on its own.

Each dump passes all four checksums, and typing its sticker values into the tool reproduces the cell
block byte for byte.


## PM200 - `PM200.BIN`

ID **694337** · capacity 210.09 g · display step 0.001 g · calibration weight 100 g

Span TC -330 ppm/°C · L0 -2229 · SPAN 2238080 · UCAL -624

Parameter sticker:

```
ID 694337
00: 44730
01: 984960
02: 488252
03: 722094
04: 393983
05: 278277
06: 1373
07: 981929
08: 102143
09: 262131
10: 131610
11: 908288
12: 917268
13: 801854
14: 513789
15: 720908
16: 784203
17: 52991
18: 983040
19: 263980
20: 694784
```

## PM3000 - `PM3000.BIN`

ID **824231** · capacity 3100.9 g · display step 0.1 g · calibration weight 2000 g

Span TC -303 ppm/°C · L0 17509 · SPAN 431557 · UCAL 2531

Parameter sticker:

```
ID 824231
00: 1027770
01: 853888
02: 241468
03: 66732
04: 918131
05: 320773
06: 1373
07: 589533
08: 487935
09: 655357
10: 787073
11: 721920
12: 917286
13: 881941
14: 772349
15: 327695
16: 869477
17: 63232
18: 65537
19: 327999
20: 318720
```

## PM4600 - `PM4600.BIN`

ID **874543** · capacity 4100.9 g · display step 0.01 g · calibration weight 1000 g
· DeltaRange: above 600 g step 0.1 g

Span TC -309 ppm/°C · L0 3003 · SPAN 4332360 · UCAL 3094

Parameter sticker:

```
ID 874543
00: 306874
01: 460672
02: 341052
03: 263354
04: 659849
05: 910597
06: 197998
07: 326074
08: 486143
09: 720878
10: 197208
11: 621824
12: 1048348
13: 682178
14: 739325
15: 196620
16: 527291
17: 232192
18: 655365
19: 581909
20: 886527
```
  
  *Lines 09 and 11 were unreadable on this faded sticker; the values shown are the ones actually stored in the EEPROM.*

## PM6000 - `PM6000.BIN`

ID **850932** · capacity 6109 g · display step 0.1 g · calibration weight 2000 g

Span TC -317 ppm/°C · L0 -21405 · SPAN 641638 · UCAL -309

Parameter sticker:

```
ID 850932
00: 1027770
01: 853888
02: 357692
03: 787650
04: 792002
05: 584965
06: 656750
07: 650896
08: 855295
09: 196557
10: 526362
11: 402688
12: 720669
13: 874239
14: 238333
15: 524305
16: 1027171
17: 612351
18: 851979
19: 392088
20: 129535
```
