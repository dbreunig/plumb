from __future__ import annotations

import dspy


class QuestionSynthesizerSignature(dspy.Signature):
    """Given a decision statement, synthesize a clear plain-English question
    that frames the decision for a developer. The question should capture
    the trade-off or choice that was made."""

    decision: str = dspy.InputField(desc="A decision statement")
    question: str = dspy.OutputField(desc="A plain-English question framing the decision")


class QuestionSynthesizer(dspy.Module):
    def __init__(self):
        super().__init__()
        self.predict = dspy.Predict(QuestionSynthesizerSignature)

    def forward(self, decision: str) -> str:
        result = self.predict(decision=decision)
        return result.question
