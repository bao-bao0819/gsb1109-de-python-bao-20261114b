typedef unsigned int uint32;
typedef unsigned char uint8;

struct LinkR {
    int code;
    struct LinkR *next;
};

typedef struct {
    uint32 timestamp;
    uint8 severity;
    struct LinkR *chain;
} Payload;
