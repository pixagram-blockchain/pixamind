-- Views HAF creates in an application's schema when its context is created
-- (hive.app_create_context). Loaded after the hivemind_app schema exists.
CREATE VIEW hivemind_app.blocks_view AS
    SELECT num, hash, prev, created_at, producer_account_id FROM hafd.blocks;
CREATE VIEW hivemind_app.operations_view AS
    SELECT id, block_num, trx_in_block, op_pos, op_type_id, body_value, custom_json_type_id FROM hafd.operations;
CREATE VIEW hivemind_app.accounts_view AS
    SELECT id, name, block_num FROM hafd.accounts;
CREATE VIEW hivemind_app.context_data_view AS
    SELECT current_block_num, irreversible_block, fork_id FROM hafd.contexts WHERE name = 'hivemind_app';
