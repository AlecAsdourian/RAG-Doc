# HNSW Construction Example

## Insert c1 (layer 0)

```
Layer 0: [c1]
```
No neighbors, no edges.

---

## Insert c2 (layer 2)

Similarities: sim(c2, c1) = 0.8

**Layer 2:**
```
[c2]  ← Entry point
```

**Layer 1:**
```
[c2]
```

**Layer 0:**
Search from entry (c2 itself at higher layers)
Find neighbors: [c1(0.8)]
Connect to m=2 nearest: [c1]

```
[c1]────[c2]
   0.8
```

---

## Insert c3 (layer 0)

Similarities: sim(c3, c1) = 0.3, sim(c3, c2) = 0.4

**Entry point:** c2 (at layer 2)

**Search process:**
1. Start at layer 2: Only c2, distance = 0.4
2. Drop to layer 1: Only c2, distance = 0.4
3. Drop to layer 0:
   - From c2, explore neighbors
   - c2 → c1 (distance to c3: 0.3)
   - Found candidates: [c2(0.4), c1(0.3)]

**Connect c3:**
Select m=2 nearest: [c2(0.4), c1(0.3)]

**Layer 0:**
```
[c1]────[c2]
 │  0.8  │
 │       │
0.3     0.4
 │       │
 └──[c3]─┘
```

---

## Insert c4 (layer 1)

Similarities:
- sim(c4, c1) = 0.6
- sim(c4, c2) = 0.7
- sim(c4, c3) = 0.5

**Entry:** c2 at layer 2

**Layer 1:**
Search from c2 (only vector here)
Nearest: [c2(0.7)]
Connect: [c2]

```
[c2]────[c4]
   0.7
```

**Layer 0:**
Search from c2, explore graph
Candidates found: [c2(0.7), c1(0.6), c3(0.5)]
Connect to m=2 nearest: [c2(0.7), c1(0.6)]

```
[c1]────[c2]────[c4]
 │  0.8  │  0.7  ↑
 │       │       │
0.3│    0.4│    0.6
 │       │       │
 └──[c3]─┘       │
      └──────────┘
       0.5
```

Wait, c1 now has 3 edges: c2(0.8), c3(0.3), c4(0.6)
Prune to m=2: Keep [c2(0.8), c4(0.6)], remove [c3(0.3)]

**After pruning:**
```
[c1]────[c2]────[c4]
 │  0.8  │  0.7  ↑
 │       │       │
 │      0.4│    0.6
 │       │       │
 └──────[c3]─────┘
    0.5
```

---

## Insert c5 (layer 0)

Similarities:
- sim(c5, c1) = 0.4
- sim(c5, c2) = 0.5
- sim(c5, c3) = 0.9 ← Very similar!
- sim(c5, c4) = 0.6

**Entry:** c2 at layer 2

**Search:**
1. Layer 2: c2 (0.5)
2. Layer 1: c2 (0.5) → c4 (0.6) ← Better!
3. Layer 0: From c4, explore
   - c4 → c2 (0.5)
   - c4 → c1 (0.4)
   - c2 → c3 (0.9) ← Best!

Candidates: [c3(0.9), c4(0.6), c2(0.5), c1(0.4)]
Connect to m=2 nearest: [c3(0.9), c4(0.6)]

**Final Layer 0:**
```
[c1]────[c2]────[c4]
 │  0.8  │  0.7  │\
 │       │       │ 0.6
 │      0.4│     │  \
 │       │       │   \
 └──────[c3]─────┘   [c5]
    0.5  └───────────┘
            0.9
```

---

## Insert c6 (layer 1)

Similarities:
- sim(c6, c1) = 0.2
- sim(c6, c2) = 0.3
- sim(c6, c3) = 0.7
- sim(c6, c4) = 0.4
- sim(c6, c5) = 0.8 ← Very similar!

**Layer 1:**
From c2, explore layer 1
Found: [c2(0.3), c4(0.4)]
Connect: [c4(0.4), c2(0.3)]

```
[c2]────[c4]────[c6]
   0.7     0.4
```

**Layer 0:**
Search finds: [c5(0.8), c3(0.7), c4(0.4), c2(0.3)]
Connect to m=2 nearest: [c5(0.8), c3(0.7)]

**Final Graph:**

```
Layer 2:  [c2]  ← Entry point

Layer 1:  [c2]────[c4]────[c6]
             0.7     0.4

Layer 0:  [c1]────[c2]────[c4]
           │  0.8  │  0.7  │
           │       │       │
           │      0.4│     0.6
           │       │       │
           └──────[c3]─────┴──[c5]────[c6]
              0.5  │   0.9     0.8
                   └───────────┘
                       0.7
```
