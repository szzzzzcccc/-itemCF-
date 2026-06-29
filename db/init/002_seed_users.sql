INSERT INTO app_users (username, display_name, password_hash, movielens_user_id)
VALUES
    ('user101', 'Xiao Chen', '2bd2a833384945be5a4d05109f418acbc78cc41d7640842f0e881ba892651296', 1),
    ('user202', 'Xiao Lin', 'b5cef39c429c739f715624213d975b1b5faca8323f2f0a2efbb98f39c5a44c09', 2),
    ('user303', 'Xiao Yu', 'd924bfbbcca7ada7599a1ef13ac11caf6a732e46306e5310b03c1b6563633bb2', 3),
    ('user000', 'New User', 'd4429c5a85da5b01e74aad071d73a8b7fb1158b3d410b6444e5dde541410b110', 611)
ON CONFLICT (username) DO UPDATE
SET
    display_name = EXCLUDED.display_name,
    password_hash = EXCLUDED.password_hash,
    movielens_user_id = EXCLUDED.movielens_user_id;
