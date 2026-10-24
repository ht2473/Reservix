Expert weights reflect a view of importance. The entropy method answers a
different question: which criterion actually distinguishes candidates. These are
not the same, and the difference between them is meaningful.

## The idea

For each criterion, the entropy of the distribution of its values across
candidates is calculated. The closer the values are to each other, the higher
the entropy and the lower the criterion's discriminating power. The lower the
entropy, the higher the weight.

The extreme case is illustrative: if all candidates have the same tenure, the
tenure criterion does not help choose anyone, and its weight drops to zero no
matter how important tenure is in the committee's view.

## What the method does not know

Importance. A criterion can distinguish candidates strongly and yet have nothing
to do with success in the position — the method cannot tell. That is why
entropy weights are best used as a check on expert weights, not as a
replacement.

## A practical caveat

The method requires sufficient data coverage. A criterion measured for three
candidates out of forty produces meaningless entropy, and such criteria are
excluded from the calculation with the reason stated.
