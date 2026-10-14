"""training assessments, attempts and answers

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-15

Step 3 of the training platform (``docs/training-platform-design.md``). Purely additive: six new
tables, no existing table or constraint is altered.

**Authoring side** (editable by an administrator, freely replaced):

* ``assessments`` - at most one per module (``uq_assessments_module_id``), carrying the pass
  threshold (``threshold_percent``, 1-100). The threshold lives *here*, not in configuration, so
  it can be read back for what it was.
* ``assessment_questions`` / ``assessment_options`` - single-correct multiple choice only.
* ``assessment_answer_keys`` - the correct option of each question, in a **table of its own**
  (primary key ``question_id``: exactly one per question; the composite foreign key makes the
  correct option one of that question's own). A query that returns options therefore cannot return
  the key by accident, and an employee-facing read never touches this table.

**History side** (written once, never changed):

* ``assessment_attempts`` - one row per *submitted* attempt. An attempt is only ever INSERTed,
  already graded: there is no in-progress row to update. It stores the threshold and the integer
  score it was graded with (``correct_count``, ``question_count``, ``threshold_percent``) and
  ``passed``, which a CHECK ties to the integers (``correct_count * 100 >= threshold_percent *
  question_count``) so the row cannot disagree with itself. ``idempotency_key`` is unique per user:
  a retried submission finds its attempt instead of creating a second.
* ``assessment_attempt_answers`` - the exact question set the attempt was graded against: each
  row holds the question's prompt, its options and the correct option **as they were**, plus the
  option chosen. They deliberately have *no* foreign key to the authoring tables, so editing or
  replacing an assessment later cannot change or re-grade what is recorded here.

Both history tables carry the ``reject_mutation()`` trigger (as ``audit_logs`` does): any UPDATE or
DELETE of a row is refused by the database, whatever the application does.

**Why every foreign key from the history side to ``training_modules`` is ``ON DELETE RESTRICT``.**
Learner history must outlive attempts to tidy up after it. A module (or, through it, a course) that
anyone has attempted can therefore not be deleted: the foreign key refuses, and the service turns
that into a 409 telling the administrator to unpublish or archive instead. This is the same
reasoning as migration 0018, which found the opposite mistake - two ``RESTRICT`` keys that made
deleting a project with claims fail - and changed them to ``CASCADE`` because those rows were
*derived* data. Attempts are not derived: they are the record, so ``RESTRICT`` is deliberate here
(attempts and answers also ``RESTRICT`` against ``users`` and ``assessments``). Only the *authoring*
rows (assessment, questions, options, keys) cascade from their module, so deleting a never-attempted
module takes its draft assessment with it.

Enumerations would be text columns with CHECK constraints (rule 7); there are none here beyond the
checks on numbers. ``downgrade`` drops the six tables in dependency order, and works with attempt
data present (``DROP TABLE`` does not fire the row triggers).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE assessments (
            id                uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            module_id         uuid        NOT NULL,
            threshold_percent integer     NOT NULL,
            created_by        uuid        NOT NULL,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_assessments_module_id_training_modules
                FOREIGN KEY (module_id) REFERENCES training_modules (id) ON DELETE CASCADE,
            CONSTRAINT fk_assessments_created_by_users
                FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_assessments_module_id UNIQUE (module_id),
            CONSTRAINT ck_assessments_threshold_percent
                CHECK (threshold_percent BETWEEN 1 AND 100)
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_assessments_set_updated_at
            BEFORE UPDATE ON assessments
            FOR EACH ROW EXECUTE FUNCTION set_updated_at();
        """
    )

    op.execute(
        """
        CREATE TABLE assessment_questions (
            id            uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            assessment_id uuid        NOT NULL,
            position      integer     NOT NULL,
            prompt        text        NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_assessment_questions_assessment_id_assessments
                FOREIGN KEY (assessment_id) REFERENCES assessments (id) ON DELETE CASCADE,
            CONSTRAINT uq_assessment_questions_assessment_id_position
                UNIQUE (assessment_id, position),
            CONSTRAINT ck_assessment_questions_position_positive CHECK (position >= 1),
            CONSTRAINT ck_assessment_questions_prompt_not_blank
                CHECK (length(btrim(prompt)) > 0)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE assessment_options (
            id          uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            question_id uuid        NOT NULL,
            position    integer     NOT NULL,
            label       text        NOT NULL,
            CONSTRAINT fk_assessment_options_question_id_assessment_questions
                FOREIGN KEY (question_id) REFERENCES assessment_questions (id) ON DELETE CASCADE,
            CONSTRAINT uq_assessment_options_question_id_position UNIQUE (question_id, position),
            CONSTRAINT uq_assessment_options_question_id_id UNIQUE (question_id, id),
            CONSTRAINT ck_assessment_options_position_positive CHECK (position >= 1),
            CONSTRAINT ck_assessment_options_label_not_blank CHECK (length(btrim(label)) > 0)
        );
        """
    )

    op.execute(
        """
        CREATE TABLE assessment_answer_keys (
            question_id       uuid PRIMARY KEY,
            correct_option_id uuid NOT NULL,
            CONSTRAINT fk_assessment_answer_keys_question_id_assessment_questions
                FOREIGN KEY (question_id) REFERENCES assessment_questions (id) ON DELETE CASCADE,
            CONSTRAINT fk_assessment_answer_keys_option_of_the_question
                FOREIGN KEY (question_id, correct_option_id)
                REFERENCES assessment_options (question_id, id) ON DELETE CASCADE
        );
        """
    )

    op.execute(
        """
        CREATE TABLE assessment_attempts (
            id                uuid        PRIMARY KEY DEFAULT uuid_generate_v7(),
            assessment_id     uuid        NOT NULL,
            module_id         uuid        NOT NULL,
            user_id           uuid        NOT NULL,
            attempt_number    integer     NOT NULL,
            threshold_percent integer     NOT NULL,
            correct_count     integer     NOT NULL,
            question_count    integer     NOT NULL,
            passed            boolean     NOT NULL,
            idempotency_key   text        NOT NULL,
            submitted_at      timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_assessment_attempts_assessment_id_assessments
                FOREIGN KEY (assessment_id) REFERENCES assessments (id) ON DELETE RESTRICT,
            CONSTRAINT fk_assessment_attempts_module_id_training_modules
                FOREIGN KEY (module_id) REFERENCES training_modules (id) ON DELETE RESTRICT,
            CONSTRAINT fk_assessment_attempts_user_id_users
                FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_assessment_attempts_module_id_user_id_attempt_number
                UNIQUE (module_id, user_id, attempt_number),
            CONSTRAINT uq_assessment_attempts_user_id_idempotency_key
                UNIQUE (user_id, idempotency_key),
            CONSTRAINT ck_assessment_attempts_attempt_number_positive CHECK (attempt_number >= 1),
            CONSTRAINT ck_assessment_attempts_threshold_percent
                CHECK (threshold_percent BETWEEN 1 AND 100),
            CONSTRAINT ck_assessment_attempts_question_count_positive CHECK (question_count >= 1),
            CONSTRAINT ck_assessment_attempts_correct_count_within
                CHECK (correct_count BETWEEN 0 AND question_count),
            CONSTRAINT ck_assessment_attempts_passed_matches_score
                CHECK (passed = (correct_count * 100 >= threshold_percent * question_count))
        );
        """
    )
    op.execute(
        "CREATE INDEX ix_assessment_attempts_passed ON assessment_attempts (user_id, module_id) "
        "WHERE passed;"
    )
    op.execute("CREATE INDEX ix_assessment_attempts_module_id ON assessment_attempts (module_id);")
    op.execute(
        """
        CREATE TRIGGER trg_assessment_attempts_reject_mutation
            BEFORE UPDATE OR DELETE ON assessment_attempts
            FOR EACH ROW EXECUTE FUNCTION reject_mutation();
        """
    )

    op.execute(
        """
        CREATE TABLE assessment_attempt_answers (
            attempt_id         uuid    NOT NULL,
            position           integer NOT NULL,
            question_id        uuid    NOT NULL,
            prompt             text    NOT NULL,
            options            jsonb   NOT NULL,
            correct_option_id  uuid    NOT NULL,
            selected_option_id uuid    NOT NULL,
            is_correct         boolean NOT NULL,
            CONSTRAINT pk_assessment_attempt_answers PRIMARY KEY (attempt_id, position),
            CONSTRAINT fk_assessment_attempt_answers_attempt_id_assessment_attempts
                FOREIGN KEY (attempt_id) REFERENCES assessment_attempts (id) ON DELETE RESTRICT,
            CONSTRAINT ck_assessment_attempt_answers_position_positive CHECK (position >= 1),
            CONSTRAINT ck_assessment_attempt_answers_options_is_array
                CHECK (jsonb_typeof(options) = 'array'),
            CONSTRAINT ck_assessment_attempt_answers_is_correct_matches
                CHECK (is_correct = (selected_option_id = correct_option_id))
        );
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_assessment_attempt_answers_reject_mutation
            BEFORE UPDATE OR DELETE ON assessment_attempt_answers
            FOR EACH ROW EXECUTE FUNCTION reject_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS assessment_attempt_answers;")
    op.execute("DROP TABLE IF EXISTS assessment_attempts;")
    op.execute("DROP TABLE IF EXISTS assessment_answer_keys;")
    op.execute("DROP TABLE IF EXISTS assessment_options;")
    op.execute("DROP TABLE IF EXISTS assessment_questions;")
    op.execute("DROP TABLE IF EXISTS assessments;")
