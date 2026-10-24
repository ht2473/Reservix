With six criteria, a person can assign weights directly, but there is nothing
to check those weights against. An expert will say "competency match is three
times more important than management experience" and a minute later
"management experience is twice as important as education, and education is
more important than competency match" — and a contradiction in such a chain
cannot be spotted by eye.

## How the method works

The expert compares criteria in pairs on a nine-point scale: one means equally
important, nine means the first is absolutely more important than the second.
The result is a square matrix whose elements are reciprocal across the main
diagonal. Weights are calculated as the components of the principal eigenvector
of this matrix.

## The key difference

The method gives not only weights but also a number describing how
contradictory the judgments are — the consistency ratio. It is compared with a
threshold: a value above 0.1 means the judgments are incompatible with each
other, and weights from such a matrix must not be used.

It is the consistency check that sets the method apart from direct assignment.
The expert does not get a refusal but an indication of which judgments
contradict the rest, and refines them.

## Limitation

The method formalizes judgment but does not replace it. It does not know the
subject area and cannot say what matters; it only prevents asserting
incompatible things.
