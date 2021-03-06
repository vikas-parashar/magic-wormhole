
-- note: anything which isn't an boolean, integer, or human-readable unicode
-- string, (i.e. binary strings) will be stored as hex

CREATE TABLE `version`
(
 `version` INTEGER -- contains one row, set to 1
);

CREATE TABLE `messages`
(
 `channel_id` INTEGER,
 `side` VARCHAR,
 `phase` VARCHAR, -- not numeric, more of a PAKE-phase indicator string
 `body` VARCHAR,
 `when` INTEGER
);
CREATE INDEX `messages_idx` ON `messages` (`channel_id`, `side`, `phase`);

CREATE TABLE `allocations`
(
 `channel_id` INTEGER,
 `side` VARCHAR
);
CREATE INDEX `allocations_idx` ON `allocations` (`channel_id`);
