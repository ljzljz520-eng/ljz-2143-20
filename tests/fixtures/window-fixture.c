#include <stdio.h>
#include <stdlib.h>
#include <string.h>

const char *vdv_greeter_message(void);

int main(void) {
    if (getenv("VDV_FIXTURE_FAIL_FIRST_FRAME") != NULL) {
        fprintf(stderr, "synthetic first-frame failure\n");
        return 7;
    }
    const char *marker = getenv("VDV_FIRST_FRAME_FILE");
    if (marker != NULL && marker[0] != '\0') {
        FILE *out = fopen(marker, "w");
        if (out == NULL) {
            perror("first-frame marker");
            return 42;
        }
        fprintf(out,
                "{\"ok\":true,\"fixture\":true,\"greeting\":\"%s\"}\n",
                vdv_greeter_message());
        fclose(out);
    } else {
        printf("%s\n", vdv_greeter_message());
    }
    return 0;
}
