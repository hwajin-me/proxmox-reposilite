// Copyright (c) 2026
// Author: hwajin-me
// License: MIT

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.DatabaseMetaData;
import java.sql.DriverManager;
import java.sql.ResultSet;

/**
 * Refuse an existing application database before Reposilite can initialize it.
 * The protected input file contains the JDBC URL and driver; neither is logged.
 */
class DatabasePreflight {
    public static void main(String[] args) {
        try {
            requireEmptyDatabase(args);
        } catch (Exception | LinkageError failure) {
            System.err.println(
                "Reposilite Database preflight failed: verify the connection and use a dedicated empty database."
            );
            System.exit(1);
        }
    }

    private static void requireEmptyDatabase(String[] args) throws Exception {
        if (args.length != 1) {
            throw new IllegalArgumentException();
        }
        JsonNode configuration = new ObjectMapper().readTree(Files.readAllBytes(Path.of(args[0])));
        JsonNode url = configuration.path("url");
        JsonNode driver = configuration.path("driver");
        if (!url.isTextual() || url.textValue().isEmpty()
                || !driver.isTextual() || driver.textValue().isEmpty()) {
            throw new IllegalArgumentException();
        }

        DriverManager.setLoginTimeout(15);
        Class.forName(driver.textValue());
        try (Connection connection = DriverManager.getConnection(url.textValue())) {
            DatabaseMetaData metadata = connection.getMetaData();
            try (ResultSet tables = metadata.getTables(
                    connection.getCatalog(), null, "%", new String[] {"TABLE"})) {
                while (tables.next()) {
                    String schema = tables.getString("TABLE_SCHEM");
                    if (!"pg_catalog".equalsIgnoreCase(schema)
                            && !"information_schema".equalsIgnoreCase(schema)) {
                        throw new IllegalStateException();
                    }
                }
            }
        }
    }
}
