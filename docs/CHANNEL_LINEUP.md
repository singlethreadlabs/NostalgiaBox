# NostalgiaBox Channel Lineup

The lineup intentionally overlaps compatible shows across specialty channels.
Physical media remains in one canonical source location. The homelab runtime
configuration in `config.homelab.yaml` points every channel directly at that
library without copying media.

| Ch. | Channel | Programming mix | Identity | Files |
|---:|---|---|---|---:|
| 2 | Slime Time Rewind | Courage the Cowardly Dog, The Fairly OddParents, KaBlam! | Messy, irreverent 1990s comedy | 277 |
| 3 | Orbit 2000 | Lilo & Stitch, The Proud Family, Sonic X | Fast, bright early-2000s animation | 148 |
| 4 | Little Sprout Playhouse | Allegra's Window, Blue's Clues, Dragon Tales, Little Bill | Gentle preschool stories and play | 293 |
| 5 | Bright Minds TV | Arthur, Gullah Gullah Island, The Magic School Bus | School-age learning and curiosity | 179 |
| 6 | Cozy Corner | Allegra's Window, Dragon Tales, Little Bill, Winnie the Pooh | Quiet preschool comfort | 153 |
| 7 | Cartoon Lab | Courage the Cowardly Dog, Dexter's Laboratory, The Fairly OddParents, KaBlam! | Creator-driven experimental comedy | 358 |
| 8 | City Toons | American Dragon: Jake Long, Kim Possible, The Proud Family, Teenage Mutant Ninja Turtles (2003) | Urban after-school adventure | 227 |
| 9 | Nova Action | Dragon Ball, Dragon Ball Z, Pokémon, Sonic X, Spider-Man Unlimited, Toonami blocks | Anime, Toonami, and science-fiction action | 225 |
| 10 | Wonder Channel | Dragon Tales, Lilo & Stitch, Super Mario World, The Legend of Zelda, Winnie the Pooh, Wizards of Waverly Place | Magical comedy and fantasy adventure | 196 |
| 11 | Studio Live | Even Stevens, Hannah Montana, Lizzie McGuire, Phil of the Future, The Suite Life, That's So Raven, Wizards of Waverly Place | Live-action school, family, and music | 308 |
| 12 | Saturday Signal | Courage, Dexter's Laboratory, Iron Man, KaBlam!, Recess, Super Mario World, Zelda, X-Men | Classic Saturday animation ritual | 327 |
| 13 | Powerhouse Kids | American Dragon, Iron Man, Kim Possible, Spider-Man Unlimited, TMNT, X-Men | Western superhero and action-comedy | 290 |
| 14 | Sick Day TV | Arthur, Blue's Clues, Dragon Tales, Nick Jr. archive, Winnie the Pooh | Familiar comfort viewing and archival daytime blocks | 594 |
| 15 | Saturday Club | Cross-network mix of 18 animated series | Broad weekend variety | 1,036 |

Regenerate the local symlink view after adding or renaming media:

```bash
python3 scripts/channel-shuffle.py
```

Before deploying, update `config.homelab.yaml` and recalculate this table from
the canonical `media/` tree. Paths inside the container use `/media/...`.

Each configured channel also uses its matching authored bumper from
`/media/Extras/NostalgiaBox-Bumpers`. `magic-channel.mp4` is retained as an
unassigned legacy asset.
