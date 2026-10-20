# Long-audio transcription: D47 check

The 2026-10-19 check sent public speech recordings through an isolated Azizam gateway to the same transcription
engine for both planners. Human transcripts supplied the reference words. The source run and its raw aggregate output
are in `~/Documents/azizam/stt-seam-eval/runs/2026-10-19/REPORT.md`. The checked source and conversion recipes are in
`registry/stt_samples.yml`; audio and transcripts are fetched on demand and are not bundled with Azizam.

The four recordings cover 104.6 minutes, three corpora, and 19 speakers or channels: Earnings-21 4366522,
ICSI Bdb001 (first 30 minutes), and AMI TS3003a and ES2014a. Each recording was run at two starting offsets,
then with the cut grid shifted by half the 30-second target. A cut's word errors within two seconds were compared
with the same words away from a cut in the paired run. Intervals resample those places 4,000 times.

| Measure | Previous silence planner | D47 energy planner |
| --- | ---: | ---: |
| Excess word error near cuts, percentage points | +1.08 [−0.33, +2.45], 725 places | −0.12 [−1.44, +1.14], 761 places |
| Noise floor away from cuts, percentage points | +0.15 [−1.30, +1.56], 712 places | −0.04 [−1.17, +1.04], 764 places |
| Longest piece sent, seconds | 58.14 | 34.95 |
| Upstream requests | 823 | 835 |
| Wall time for this run, seconds | 535 | 313 |

The D47 seam estimate includes zero and sits within its noise floor. No piece exceeded the configured 40-second
maximum. The earlier planner exceeded that maximum. The difference between planners, +1.21 [−0.63, +3.12]
percentage points, also includes zero: this check verifies the hard bound and finds no measurable seam penalty,
but does not establish that D47 has better recognition quality on these recordings.

| Recording | Previous excess, percentage points | D47 excess, percentage points |
| --- | ---: | ---: |
| Earnings-21 4366522 | +0.46 [−0.72, +1.64] | +1.08 [+0.00, +2.18] |
| ICSI Bdb001 | +0.93 [−1.65, +3.50] | −1.50 [−4.14, +1.17] |
| AMI TS3003a | +0.78 [−2.35, +3.61] | +0.08 [−2.38, +2.27] |
| AMI ES2014a | +2.93 [−1.35, +6.92] | +1.33 [−1.53, +4.20] |

In a separate piece-length sweep through the gateway, deletion rates from 20 through 60 seconds did not show a
clear increase on these public recordings. The 40-second maximum is therefore a safety bound for the measured
engine; its quality benefit on these corpora remains unproven. Earnings-21 word times are interpolated within
speaker turns. Meeting transcripts contain backchannels that the engine often omits, which raises absolute deletion
rates and widens the intervals. The operator measurement tool reports its own data and asks the owner to apply any
recommended values; these measured values are not constants in gateway code.
