from app.database import connect_database


def main() -> None:
    connection = connect_database()

    try:
        database_result = connection.execute("SELECT 1").fetchone()

        vector_result = connection.execute(
            "SELECT vector_distance_cos(vector32(?), vector32(?))",
            ("[1,0]", "[1,0]"),
        ).fetchone()
    finally:
        connection.close()

    assert database_result == (1,)
    assert vector_result == (0.0,)

    print("Turso and vector functions are ready.")

if __name__ == "__main__":
    main()