ALTER TABLE agent.tasks DROP CONSTRAINT task_schedule_fk;
DROP TABLE agent.schedule_occurrences;
DROP TABLE agent.schedules;
DROP SCHEMA queue CASCADE;
