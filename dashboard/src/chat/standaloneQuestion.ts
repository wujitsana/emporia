/** The prompts end in ": " so the person can finish the sentence; a pasted question must stand alone. */
export function standaloneQuestion(prompt: string): string {
  return prompt.trimEnd().endsWith(":") ? `${prompt}what happened here, and why did you decide as you did?` : prompt;
}
