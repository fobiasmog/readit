## Purpose

Lets a Claude Code user hear a short, human-style spoken explanation of what happened in the current session — the last turn, the last N turns, or the whole session — synthesized locally and played in the background without adding to the session's context.

## ADDED Requirements

### Requirement: Scope selection
The skill SHALL accept an optional scope argument: none or `last` for the last complete turn, a positive integer N for the last N complete turns, and `all` for every turn in the session. A turn SHALL consist of one user prompt and everything Claude did in response to it, up to the next user prompt.

#### Scenario: Default scope is the last turn
- **WHEN** the user runs `/listen` with no argument
- **THEN** the spoken explainer covers only the most recent complete turn before the `/listen` request

#### Scenario: Last N turns
- **WHEN** the user runs `/listen 3` in a session with at least three complete turns
- **THEN** the spoken explainer covers exactly the three most recent complete turns

#### Scenario: N exceeds available turns
- **WHEN** the user runs `/listen 10` in a session with four complete turns
- **THEN** the spoken explainer covers all four turns and says that only four were available

#### Scenario: Whole session
- **WHEN** the user runs `/listen all`
- **THEN** the spoken explainer covers every complete turn of the current session

#### Scenario: Invalid argument
- **WHEN** the user runs `/listen` with an argument that is not `last`, `all`, `stop`, or a positive integer
- **THEN** no audio is generated and the user receives a one-line message listing the valid arguments

### Requirement: Scope excludes listen activity
Scope selection SHALL ignore the in-progress `/listen` request, earlier `/listen` requests, and notifications produced by earlier `/listen` runs.

#### Scenario: Repeated listen
- **WHEN** the user runs `/listen` twice in a row with no other prompt in between
- **THEN** both runs cover the same turn, the last turn that is not a `/listen` request

### Requirement: Content coverage
The explainer SHALL be based on the user's prompts, Claude's visible replies, and the content of files Claude created or edited within the scope. It SHALL NOT be based on Claude's hidden reasoning or raw tool output.

#### Scenario: Artifact written to a file
- **WHEN** the last turn created an OpenSpec proposal file and Claude's visible reply only listed the created files
- **THEN** the spoken explainer describes the content of the proposal, not just the fact that files were created

### Requirement: Whole-session fidelity after compaction
The whole-session scope SHALL include turns that occurred before the session's context was compacted.

#### Scenario: Compacted session
- **WHEN** the session has been compacted and the user runs `/listen all`
- **THEN** the explainer covers topics from turns before the compaction point as well as after it

### Requirement: Oversized scope handling
When the selected scope exceeds the processing size budget, the system SHALL still produce an explainer. It SHALL keep the most recent turn in full, condense or omit older material first, and the explainer SHALL say that earlier parts were condensed.

#### Scenario: Very long session
- **WHEN** the user runs `/listen all` on a session whose content exceeds the size budget
- **THEN** an explainer is produced, the most recent turn is fully represented, and the narration mentions that earlier parts were condensed

### Requirement: Solo spoken explainer
The system SHALL produce a single-narrator, conversational script that explains the material the way a person would explain an idea aloud. Unless configured otherwise, it SHALL target about two minutes of speech. It SHALL NOT read markdown syntax, code, tables, diagrams, or raw file paths literally.

#### Scenario: Explainer style
- **WHEN** an explainer is produced for a turn that contains headings, a table, a code block, and file paths
- **THEN** the narration is continuous prose from one voice, covers what the material is, why it matters, and how it works, and does not speak markup symbols, code, or full paths

#### Scenario: Configured length
- **WHEN** the configured word budget is changed
- **THEN** subsequent explainers target the new length

### Requirement: Local speech synthesis
Speech SHALL be synthesized on the user's machine with the Orpheus 3B fine-tuned model, and synthesis SHALL NOT require network access once model files are installed. The model quantization (`bf16`, `8bit`, `6bit`, `4bit`) and the voice SHALL be configurable, and the default voice SHALL be `tara`.

#### Scenario: Offline synthesis
- **WHEN** the model files are installed and the machine has no network connection
- **THEN** speech for an already-written script is still synthesized and played

#### Scenario: Configured quantization
- **WHEN** the configured quantization is set to `4bit`
- **THEN** the next synthesis uses the 4-bit model build

### Requirement: Context isolation
Reading the session, writing the script, and synthesizing audio SHALL happen outside the invoking session's context. The invoking session SHALL receive at most one short notification per `/listen` request, and SHALL NOT receive transcript content, the script, or audio data.

#### Scenario: Main context stays clean
- **WHEN** the user runs `/listen all` on a long session
- **THEN** the invoking session gains only the command and a notification of at most two lines naming the scope, estimated duration, and quantization

### Requirement: Non-blocking background playback
`/listen` SHALL return control to the user without waiting for synthesis or playback. Audio SHALL start automatically when ready, and the user SHALL get a system notification when playback starts.

#### Scenario: Keep working while it generates
- **WHEN** the user runs `/listen` and sends another prompt before the audio is ready
- **THEN** the new prompt is handled normally and the audio still plays when ready

#### Scenario: Playback start notice
- **WHEN** synthesis completes
- **THEN** a system notification appears and the audio begins playing

### Requirement: Single active playback and stop
Only one `/listen` run SHALL generate or play at a time. A new run SHALL cancel any active one. `/listen stop` SHALL cancel any active generation or playback.

#### Scenario: New run replaces old
- **WHEN** a `/listen` run is generating or playing and the user starts another `/listen`
- **THEN** the earlier run stops and only the new run's audio plays

#### Scenario: Stop while playing
- **WHEN** audio is playing and the user runs `/listen stop`
- **THEN** playback stops within a few seconds and no further audio from that run plays

#### Scenario: Stop with nothing active
- **WHEN** nothing is generating or playing and the user runs `/listen stop`
- **THEN** the user is told there was nothing to stop

### Requirement: Saved outputs
Each run SHALL save its script and audio locally with a timestamped name, and the system SHALL keep only a configurable number of the most recent runs.

#### Scenario: Replay later
- **WHEN** a run completes
- **THEN** its script text and audio file exist in the output directory and can be replayed without regenerating

#### Scenario: Retention
- **WHEN** a new run completes and the number of saved runs exceeds the configured limit
- **THEN** the oldest runs' files are deleted

### Requirement: Failure reporting
If any step fails (transcript not found, model missing, synthesis failure, or runaway generation), the system SHALL NOT play partial or corrupted audio, and it SHALL tell the user the reason in one line.

#### Scenario: Model not installed
- **WHEN** the configured model build is not installed and the network is unavailable
- **THEN** no audio plays and the user is notified that the model is missing, naming the build

#### Scenario: Runaway generation
- **WHEN** synthesis of a chunk does not terminate within its expected length after a retry
- **THEN** the run is aborted and the user is notified of the failure instead of hearing garbled audio
